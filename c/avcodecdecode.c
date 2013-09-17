/*
#   avcodecdecode.c: decodes wma file format for xlplayer
#   Copyright (C) 2007, 2011 Stephen Fairchild (s-fairchild@users.sourceforge.net)
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program in the file entitled COPYING.
#   If not, see <http://www.gnu.org/licenses/>.
*/

#include "../config.h"

#ifdef HAVE_AVCODEC
#ifdef HAVE_AVFORMAT
#ifdef HAVE_AVFILTER

#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include "main.h"
#include "xlplayer.h"
#include "avcodecdecode.h"

#define TRUE 1
#define FALSE 0
#define ACCEPTED 1
#define REJECTED 0

extern int dynamic_metadata_form[];

static const struct timespec time_delay = { .tv_nsec = 10 };

static int init_filters(struct avcodecdecode_vars *s, const char *filters_descr)
    {
    char args[512];
    int ret;
    AVFilter *abuffersrc  = avfilter_get_by_name("abuffer");
    AVFilter *abuffersink = avfilter_get_by_name("ffabuffersink");
    AVFilterInOut *outputs = avfilter_inout_alloc();
    AVFilterInOut *inputs  = avfilter_inout_alloc();
    const enum AVSampleFormat sample_fmts[] = { AV_SAMPLE_FMT_FLT, AV_SAMPLE_FMT_S16, -1 };
    AVABufferSinkParams *abuffersink_params;
    const AVFilterLink *outlink;
    AVRational time_base = s->ic->streams[s->stream]->time_base;

    s->filter_graph = avfilter_graph_alloc();

    /* buffer audio source: the decoded frames from the decoder will be inserted here. */
    if (!s->c->channel_layout)
        s->c->channel_layout = av_get_default_channel_layout(s->c->channels);
    snprintf(args, sizeof(args),
            "time_base=%d/%d:sample_rate=%d:sample_fmt=%s:channel_layout=0x%"PRIx64,
             time_base.num, time_base.den, s->c->sample_rate,
             av_get_sample_fmt_name(s->c->sample_fmt), s->c->channel_layout);
    ret = avfilter_graph_create_filter(&s->buffersrc_ctx, abuffersrc, "in",
                                       args, NULL, s->filter_graph);
    if (ret < 0) {
        av_log(NULL, AV_LOG_ERROR, "Cannot create audio buffer source %s\n", args);
        return ret;
    }

    /* buffer audio sink: to terminate the filter chain. */
    abuffersink_params = av_abuffersink_params_alloc();
    abuffersink_params->sample_fmts     = sample_fmts;
    ret = avfilter_graph_create_filter(&s->buffersink_ctx, abuffersink, "out",
                                       NULL, abuffersink_params, s->filter_graph);
    av_free(abuffersink_params);
    if (ret < 0) {
        av_log(NULL, AV_LOG_ERROR, "Cannot create audio buffer sink\n");
        return ret;
    }

    /* Endpoints for the filter graph. */
    outputs->name       = av_strdup("in");
    outputs->filter_ctx = s->buffersrc_ctx;
    outputs->pad_idx    = 0;
    outputs->next       = NULL;

    inputs->name       = av_strdup("out");
    inputs->filter_ctx = s->buffersink_ctx;
    inputs->pad_idx    = 0;
    inputs->next       = NULL;

    if ((ret = avfilter_graph_parse(s->filter_graph, filters_descr,
                                    &inputs, &outputs, NULL)) < 0)
        return ret;

    if ((ret = avfilter_graph_config(s->filter_graph, NULL)) < 0)
        return ret;

    /* Print summary of the sink buffer
     * Note: args buffer is reused to store channel layout string */
    outlink = s->buffersink_ctx->inputs[0];
    av_get_channel_layout_string(args, sizeof(args), -1, outlink->channel_layout);
    av_log(NULL, AV_LOG_INFO, "Output: srate:%dHz fmt:%s chlayout:%s\n",
           (int)outlink->sample_rate,
           (char *)av_x_if_null(av_get_sample_fmt_name(outlink->format), "?"),
           args);

    return 0;
    }

static void avcodecdecode_eject(struct xlplayer *xlplayer)
    {
    struct avcodecdecode_vars *self = xlplayer->dec_data;
    
    avfilter_graph_free(&self->filter_graph);
    av_freep(&self->frame);
    while (pthread_mutex_trylock(&g.avc_mutex))
        nanosleep(&time_delay, NULL);
    avcodec_close(self->c);
    pthread_mutex_unlock(&g.avc_mutex);
    avformat_close_input(&self->ic);
    if (self->frame)
        av_free(self->frame);
    free(self);
    fprintf(stderr, "finished eject\n");
    }

static void avcodecdecode_init(struct xlplayer *xlplayer)
    {
    struct avcodecdecode_vars *self = xlplayer->dec_data;
    
    if (xlplayer->seek_s)
        av_seek_frame(self->ic, -1, (int64_t)xlplayer->seek_s * AV_TIME_BASE, 0);
    }
    
static void avcodecdecode_play(struct xlplayer *xlplayer)
    {
    struct avcodecdecode_vars *self = xlplayer->dec_data;

    /* write out any bufferred audio from the last go around */
    if (xlplayer->write_deferred)
        {
        xlplayer_write_channel_data(xlplayer);
        /* if failed to flush then go around again */
        if (xlplayer->write_deferred)
            return;
        }

    /* try and obtain audio data from the filter bank */
    while (1)
        {
        AVFilterBufferRef *samplesref;
        int ret;

        /* check if there is data to pull from resampler */
        ret = av_buffersink_get_buffer_ref(self->buffersink_ctx, &samplesref, 0);
        if(ret == AVERROR(EAGAIN) || ret == AVERROR_EOF)
            /* if we are here the filter bank has run dry */
            break;

        if(ret < 0)
            {
            /* unexpected error handler */
            xlplayer->playmode = PM_EJECTING;
            return;
            }

        if (samplesref)
            {
            xlplayer_demux_channel_data(xlplayer, (float *)samplesref->data[0], samplesref->audio->nb_samples, 2, 1.f);
            avfilter_unref_bufferp(&samplesref);
            xlplayer_write_channel_data(xlplayer);
            if (xlplayer->write_deferred)
                return;
            }
        }

    int got_frame = 0;
    while (!got_frame)
        {
        AVPacket packet;
        int ret;

        if (av_read_frame(self->ic, &packet) < 0)
            {
            xlplayer->playmode = PM_EJECTING;
            return;
            }

        if (packet.stream_index == self->stream)
            {
            avcodec_get_frame_defaults(self->frame);
            while (pthread_mutex_trylock(&g.avc_mutex))
                nanosleep(&time_delay, NULL);
            ret = avcodec_decode_audio4(self->c, self->frame, &got_frame, &packet);
            pthread_mutex_unlock(&g.avc_mutex);

            if (ret < 0)
                av_log(NULL, AV_LOG_ERROR, "Error decoding audio\n");
            else
                if (got_frame)
                    {
                    /* push the audio data from decoded frame into the filtergraph */
                    if (av_buffersrc_add_frame(self->buffersrc_ctx, self->frame, 0) < 0)
                        {
                        av_log(NULL, AV_LOG_ERROR, "Error while feeding the audio filtergraph\n");
                        break;
                        }
                    }
            }
        av_free_packet(&packet);
        }
    
    int delay = xlplayer_calc_rbdelay(xlplayer);
    struct chapter *chapter = mp3_tag_chapter_scan(&self->taginfo, xlplayer->play_progress_ms + delay);
    if (chapter && chapter != self->current_chapter)
        {
        self->current_chapter = chapter;
        xlplayer_set_dynamic_metadata(xlplayer, dynamic_metadata_form[chapter->title.encoding], chapter->artist.text, chapter->title.text, chapter->album.text, delay);
        }
    }

int avcodecdecode_reg(struct xlplayer *xlplayer)
    {
    struct avcodecdecode_vars *self;
    FILE *fp;
    struct chapter *chapter;
    
    if (!(xlplayer->dec_data = self = calloc(1, sizeof (struct avcodecdecode_vars))))
        {
        fprintf(stderr, "avcodecdecode_reg: malloc failure\n");
        return REJECTED;
        }
    else
        xlplayer->dec_data = self;
    
    if ((fp = fopen(xlplayer->pathname, "r")))
        {
        mp3_tag_read(&self->taginfo, fp);
        if ((chapter = mp3_tag_chapter_scan(&self->taginfo, xlplayer->play_progress_ms + 70)))
            {
            self->current_chapter = chapter;
            xlplayer_set_dynamic_metadata(xlplayer, dynamic_metadata_form[chapter->title.encoding], chapter->artist.text, chapter->title.text, chapter->album.text, 70);
            }
        fclose(fp);
        }
    
    if (avformat_open_input(&self->ic, xlplayer->pathname, NULL, NULL) < 0)
        {
        fprintf(stderr, "avcodecdecode_reg: failed to open input file %s\n", xlplayer->pathname);
        free(self);
        return REJECTED;
        }

    if (avformat_find_stream_info(self->ic, NULL) < 0)
        {
        fprintf(stderr, "avcodecdecode_reg: call to avformat_find_stream_info failed\n");
        goto fail;
        }

    while (pthread_mutex_trylock(&g.avc_mutex))
        nanosleep(&time_delay, NULL);
    if ((self->stream = av_find_best_stream(self->ic, AVMEDIA_TYPE_AUDIO, -1, -1, &self->codec, 0)) < 0)
        {
        fprintf(stderr, "Cannot find an audio stream in the input file\n");
        goto fail;
        }
    pthread_mutex_unlock(&g.avc_mutex);

    self->c = self->ic->streams[self->stream]->codec;

    while (pthread_mutex_trylock(&g.avc_mutex))
        nanosleep(&time_delay, NULL);
    if (avcodec_open2(self->c, self->codec, NULL) < 0)
        {
        pthread_mutex_unlock(&g.avc_mutex);
        fprintf(stderr, "avcodecdecode_reg: could not open codec\n");
        goto fail;
        }
    pthread_mutex_unlock(&g.avc_mutex);

    if (!(self->frame = avcodec_alloc_frame()))
        {
        fprintf(stderr, "avcodecdecode_reg: could not allocate frame\n");
        goto fail;
        }

    char filter_descr[80];
    snprintf(filter_descr, 80, "aresample=%u,aconvert=flt:stereo", xlplayer->samplerate);
    if (init_filters(self, filter_descr))
        goto fail;
    
    xlplayer->dec_init = avcodecdecode_init;
    xlplayer->dec_play = avcodecdecode_play;
    xlplayer->dec_eject = avcodecdecode_eject;
    
    return ACCEPTED;
    
    fail:
        avfilter_graph_free(&self->filter_graph);
        if (self->frame)
            av_freep(&self->frame);
        if (self->c)
            avcodec_close(self->c);
        avformat_close_input(&self->ic);
        free(self);
        return REJECTED;
    
    }
    
#endif /* HAVE_AVFILTER */
#endif /* HAVE_AVFORMAT */
#endif /* HAVE_AVCODEC */
