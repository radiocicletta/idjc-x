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

#ifdef HAVE_LIBAV

#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <libavutil/opt.h>
#include "main.h"
#include "xlplayer.h"
#include "avcodecdecode.h"

#define TRUE 1
#define FALSE 0
#define ACCEPTED 1
#define REJECTED 0

#ifndef AVCODEC_MAX_AUDIO_FRAME_SIZE
#define AVCODEC_MAX_AUDIO_FRAME_SIZE 192000
#endif

extern int dynamic_metadata_form[];

static const struct timespec time_delay = { .tv_nsec = 10 };

static void avcodecdecode_eject(struct xlplayer *xlplayer)
    {
    struct avcodecdecode_vars *self = xlplayer->dec_data;

    if (self->pkt.data)
        av_free_packet(&self->pkt);
    if (self->resample)
        {
        xlplayer->src_state = src_delete(xlplayer->src_state);
        free(xlplayer->src_data.data_out);
        }
    if (self->floatsamples)
        free(self->floatsamples);
#ifdef HAVE_SWRESAMPLE
    if (self->swr)
        swr_free(&self->swr);
#endif
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
    int src_error;
    
    if (xlplayer->seek_s)
        {
        av_seek_frame(self->ic, -1, (int64_t)xlplayer->seek_s * AV_TIME_BASE, 0);
        switch (self->c->codec_id)
            {
            case CODEC_ID_MUSEPACK7:   /* add formats here that glitch when seeked */
            case CODEC_ID_MUSEPACK8:
                self->drop = 1.6;
                fprintf(stderr, "dropping %0.2f seconds of audio\n", self->drop);
            default:
                break;
            }
        }
        
    self->channels = (self->c->channels == 1) ? 1 : 2;
    if ((self->resample = (self->c->sample_rate != (int)xlplayer->samplerate)))
        {
        fprintf(stderr, "configuring resampler\n");
        xlplayer->src_data.src_ratio = (double)xlplayer->samplerate / (double)self->c->sample_rate;
        xlplayer->src_data.end_of_input = 0;
        
        const size_t dsiz = AVCODEC_MAX_AUDIO_FRAME_SIZE * self->channels * xlplayer->src_data.src_ratio + 512;
        
        xlplayer->src_data.output_frames = dsiz / (sizeof (float) * self->channels);
        if (!(xlplayer->src_data.data_out = malloc(dsiz)))
            {
            fprintf(stderr, "avcodecdecode_init: malloc failure\n");
            self->resample = FALSE;
            avcodecdecode_eject(xlplayer);
            xlplayer->playmode = PM_STOPPED;
            xlplayer->command = CMD_COMPLETE;
            return;
            }
        if ((xlplayer->src_state = src_new(xlplayer->rsqual, self->channels, &src_error)), src_error)
            {
            fprintf(stderr, "avcodecdecode_init: src_new reports %s\n", src_strerror(src_error));
            free(xlplayer->src_data.data_out);
            self->resample = FALSE;
            avcodecdecode_eject(xlplayer);
            xlplayer->playmode = PM_STOPPED;
            xlplayer->command = CMD_COMPLETE;
            return;
            }
        }
        
    fprintf(stderr, "avcodecdecode_init: completed\n");
    }
    
static void avcodecdecode_play(struct xlplayer *xlplayer)
    {
    struct avcodecdecode_vars *self = xlplayer->dec_data;
    int channels = self->c->channels;
    SRC_DATA *src_data = &xlplayer->src_data;
    
    if (xlplayer->write_deferred)
        {
        xlplayer_write_channel_data(xlplayer);
        return;
        }
    
    if (self->size <= 0)
        {
        if (av_read_frame(self->ic, &self->pkt) < 0 || (self->size = self->pkt.size) == 0)
            {
            if (self->pkt.data)
                av_free_packet(&self->pkt);

            if (self->resample)       /* flush the resampler */
                {
                src_data->end_of_input = TRUE;
                src_data->input_frames = 0;
                if (src_process(xlplayer->src_state, src_data))
                    {
                    fprintf(stderr, "avcodecdecode_play: error occured during resampling\n");
                    xlplayer->playmode = PM_EJECTING;
                    return;
                    }
                xlplayer_demux_channel_data(xlplayer, src_data->data_out, src_data->output_frames_gen, channels, 1.f);
                xlplayer_write_channel_data(xlplayer);
                }
            xlplayer->playmode = PM_EJECTING;
            return;
            }
        self->pktcopy = self->pkt;
        }

    if (self->pkt.stream_index != (int)self->stream)
        {
        if (self->pkt.data)
            av_free_packet(&self->pkt);
        self->size = 0;
        return;
        }

    do
        {
        int len, frames, got_frame = 0;
        
        if (!self->frame)
            {
            if (!(self->frame = avcodec_alloc_frame()))
                {
                fprintf(stderr, "avcodecdecode_play: malloc failure\n");
                exit(1);
                }
            else
                avcodec_get_frame_defaults(self->frame);
            }

        while (pthread_mutex_trylock(&g.avc_mutex))
            nanosleep(&time_delay, NULL);
        len = avcodec_decode_audio4(self->c, self->frame, &got_frame, &self->pktcopy);
        pthread_mutex_unlock(&g.avc_mutex);

        if (len < 0)
            {
            fprintf(stderr, "avcodecdecode_play: error during decode\n");
            break;
            }

        self->pktcopy.data += len;
        self->pktcopy.size -= len;
        self->size -= len;

        if (!got_frame)
            {
            continue;
            }

#ifdef HAVE_SWRESAMPLE
        if (!self->swr)
            {
            int64_t layout;
            
            if (!(self->swr = swr_alloc()))
                {
                fprintf(stderr, "avcodecdecode_play: call to swr_alloc failed\n");
                xlplayer->playmode = PM_EJECTING;
                return;
                }

            layout = self->frame->channel_layout;
            if (!layout)
                layout = self->c->channel_layout;
            if (!layout)
                {
                if (!channels)
                    {
                    fprintf(stderr, "avcodecdecode_play: number of channels is zero\n");
                    xlplayer->playmode = PM_EJECTING;
                    return;
                    }
                
                layout = av_get_default_channel_layout(channels);
                }

            av_opt_set_int(self->swr, "in_channel_layout", layout, 0);
            av_opt_set_int(self->swr, "out_channel_layout", (self->channels == 2) ? AV_CH_LAYOUT_STEREO : AV_CH_LAYOUT_MONO, 0);
            av_opt_set_sample_fmt(self->swr, "in_sample_fmt", self->c->sample_fmt, 0);
            av_opt_set_sample_fmt(self->swr, "out_sample_fmt", AV_SAMPLE_FMT_FLT, 0);
            //av_opt_set_int(self->swr, "in_sample_rate",     44100,                0);
            //av_opt_set_int(self->swr, "out_sample_rate",    44100,                0);

            if (swr_init(self->swr))
                {
                fprintf(stderr, "avcodecdecode_init: swr_init failed\n");
                xlplayer->playmode = PM_EJECTING;
                return;
                }
            }

        if (self->floatsamples)
            av_freep(&self->floatsamples);

        if (av_samples_alloc(&self->floatsamples, NULL, 2, self->frame->nb_samples, AV_SAMPLE_FMT_FLT, 0))
            {
            fprintf(stderr, "avcodecdecode_play: av_samples_alloc failed\n");
            xlplayer->playmode = PM_EJECTING;
            return;
            }

        swr_convert(self->swr, &self->floatsamples, self->frame->nb_samples, (const uint8_t **)self->frame->data, self->frame->nb_samples);
#else
        
        if (!(self->floatsamples))
            {
            if (channels > 2 || channels < 1)
                {
                fprintf(stderr, "avcodecdecode_init: unhandled number of channels: %d\n", channels);
                xlplayer->playmode = PM_EJECTING;
                return;
                }
                
            if (!(self->floatsamples = malloc(sizeof (float) * self->channels * AVCODEC_MAX_AUDIO_FRAME_SIZE)))
                {
                fprintf(stderr, "avcodecdecode_init: malloc failure\n");
                xlplayer->playmode = PM_EJECTING;
                return;
                }
            }

        int buffer_size = av_samples_get_buffer_size(NULL, channels,
                            self->frame->nb_samples, self->c->sample_fmt, 1);

        switch (self->c->sample_fmt) {
            case AV_SAMPLE_FMT_FLT:
                frames = (buffer_size >> 2) / channels;
                memcpy(self->floatsamples, self->frame->data[0], buffer_size);
                break;
                
            case AV_SAMPLE_FMT_FLTP:
                frames = (buffer_size >> 2) / channels;
                {
                float *l = (float *)self->frame->data[0];
                float *r = NULL;
                if (channels == 2)
                    r = (float *)self->frame->data[1];
                float *d = self->floatsamples;
                float *endp = self->floatsamples + (channels * frames);
                while (d < endp)
                    {
                    *d++ = *l++;
                    if (channels == 2)
                        *d++ = *r++;
                    }
                }
                break;

            case AV_SAMPLE_FMT_DBL:
                frames  = (buffer_size >> 3) / channels;
                {
                double *s = (double *)self->frame->data[0];
                float *d = self->floatsamples;
                float *endp = self->floatsamples + (channels * frames);
                while (d < endp)
                    *d++ = (float)*s++;
                }
                break;

            case AV_SAMPLE_FMT_DBLP:
                frames  = (buffer_size >> 3) / channels;
                {
                double *l = (double *)self->frame->data[0];
                double *r = NULL;
                if (channels == 2)
                    r = (double *)self->frame->data[1];
                float *d = self->floatsamples;
                float *endp = self->floatsamples + (channels * frames);
                while (d < endp)
                    {
                    *d++ = (float)*l++;
                    if (channels == 2)
                        *d++ = (float)*r++;
                    }
                }
                break;
                
            case AV_SAMPLE_FMT_S16:
                 frames = (buffer_size >> 1) / channels;
                xlplayer_make_audio_to_float(xlplayer, self->floatsamples,
                                self->frame->data[0], frames, 16, channels);
                break;

            case AV_SAMPLE_FMT_S16P:
                frames = (buffer_size >> 1) / channels;
                {
                int16_t *l = (int16_t *)self->frame->data[0];
                int16_t *r = NULL;
                if (channels == 2)
                    r = (int16_t *)self->frame->data[1];
                float *d = self->floatsamples;
                float *endp = self->floatsamples + (channels * frames);
                while (d < endp)
                    {
                    *d++ = *l++ / 32768.0f;
                    if (channels == 2)
                        *d++ = *r++ / 32768.0f;
                    }
                }
                break;
                
            case AV_SAMPLE_FMT_S32:
                frames = (buffer_size >> 2) / channels;
                xlplayer_make_audio_to_float(xlplayer, self->floatsamples,
                                self->frame->data[0], frames, 32, channels);
                break;

            case AV_SAMPLE_FMT_S32P:
                frames = (buffer_size >> 2) / channels;
                {
                int32_t *l = (int32_t *)self->frame->data[0];
                int32_t *r = NULL;
                if (channels == 2)
                    r = (int32_t *)self->frame->data[1];
                float *d = self->floatsamples;
                float *endp = self->floatsamples + (channels * frames);
                while (d < endp)
                    {
                    *d++ = *l++ / 1073741824.0f;
                    if (channels == 2)
                        *d++ = *r++ / 1073741824.0f;
                    }
                }
                break;

            case AV_SAMPLE_FMT_NONE:
                fprintf(stderr, "avcodecdecode_play: sample format is none\n");
                xlplayer->playmode = PM_EJECTING;
                return;

            default:
                fprintf(stderr, "avcodecdecode_play: unexpected data format %d\n", (int)self->c->sample_fmt);
                xlplayer->playmode = PM_EJECTING;
                return;
            }
   
#endif /* HAVE_SWRESAMPLE */

        if (self->resample)
            {
            src_data->input_frames = self->frame->nb_samples;
            src_data->data_in = (float *)self->floatsamples;
            if (src_process(xlplayer->src_state, src_data))
                {
                fprintf(stderr, "avcodecdecode_play: error occured during resampling\n");
                xlplayer->playmode = PM_EJECTING;
                return;
                }
            xlplayer_demux_channel_data(xlplayer, src_data->data_out, frames = src_data->output_frames_gen, self->channels, 1.f);
            }
        else
            xlplayer_demux_channel_data(xlplayer, (float *)self->floatsamples, frames = self->frame->nb_samples, self->channels, 1.f);
            
        if (self->drop > 0)
            self->drop -= frames / (float)xlplayer->samplerate;
        else
            xlplayer_write_channel_data(xlplayer);
        } while (!xlplayer->write_deferred && self->size > 0);

    if (self->size <= 0)
        {
        if (self->pkt.data)
            av_free_packet(&self->pkt);
        int delay = xlplayer_calc_rbdelay(xlplayer);
        struct chapter *chapter = mp3_tag_chapter_scan(&self->taginfo, xlplayer->play_progress_ms + delay);
        if (chapter && chapter != self->current_chapter)
            {
            self->current_chapter = chapter;
            xlplayer_set_dynamic_metadata(xlplayer, dynamic_metadata_form[chapter->title.encoding], chapter->artist.text, chapter->title.text, chapter->album.text, delay);
            }
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
        avformat_close_input(&self->ic);
        free(self);
        return REJECTED;
        }

    while (pthread_mutex_trylock(&g.avc_mutex))
        nanosleep(&time_delay, NULL);
    if ((self->stream = av_find_best_stream(self->ic, AVMEDIA_TYPE_AUDIO, -1, -1, &self->codec, 0)) < 0)
        {
        fprintf(stderr, "Cannot find an audio stream in the input file\n");
        avformat_close_input(&self->ic);
        free(self);
        return REJECTED;
        }
    pthread_mutex_unlock(&g.avc_mutex);

    self->c = self->ic->streams[self->stream]->codec;
#ifndef HAVE_SWRESAMPLE
    self->c->request_sample_fmt = AV_SAMPLE_FMT_FLT;
    self->c->request_channel_layout = AV_CH_LAYOUT_STEREO_DOWNMIX;
#endif

    while (pthread_mutex_trylock(&g.avc_mutex))
        nanosleep(&time_delay, NULL);
    if (avcodec_open2(self->c, self->codec, NULL) < 0)
        {
        pthread_mutex_unlock(&g.avc_mutex);
        fprintf(stderr, "avcodecdecode_reg: could not open codec\n");
        avformat_close_input(&self->ic);
        free(self);
        return REJECTED;
        }
    pthread_mutex_unlock(&g.avc_mutex);

    xlplayer->dec_init = avcodecdecode_init;
    xlplayer->dec_play = avcodecdecode_play;
    xlplayer->dec_eject = avcodecdecode_eject;
    
    return ACCEPTED;
    }

#endif /* HAVE_LIBAV */
