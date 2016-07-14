#   mutagengui.py: GTK based file tagger.
#   Copyright (C) 2009 Stephen Fairchild (s-fairchild@users.sourceforge.net)
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


__all__ = ['MutagenGUI']

import os
import sys
import re
import gettext

from gi.repository import Gtk
from gi.repository import Pango
from gi.repository import GLib
import mutagen
import mutagen.id3 as id3
from mutagen.mp3 import MP3
from mutagen.apev2 import APEv2, APETextValue
from mutagen.musepack import Musepack
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.asf import ASFUnicodeAttribute

from idjc import FGlobs
from .tooltips import set_tip
from idjc.prelims import ProfileManager


t = gettext.translation(FGlobs.package_name, FGlobs.localedir, fallback=True)
_ = t.gettext

pm = ProfileManager()


class FreeTagFrame(Gtk.Frame):

    def __init__(self):
        super(FreeTagFrame, self).__init__()
        sw = Gtk.ScrolledWindow()
        sw.set_border_width(5)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
        self.add(sw)
        sw.show()
        self.tb = Gtk.TextBuffer()
        tv = Gtk.TextView.new_with_buffer(self.tb)
        tv.set_wrap_mode(Gtk.WrapMode.CHAR)
        tv.modify_font(Pango.FontDescription('sans 12'))
        sw.add(tv)
        tv.show()


class MutagenTagger(Gtk.Grid):

    """Base class for ID3Tagger and NativeTagger."""

    def __init__(self, pathname):
        super(MutagenTagger, self).__init__()
        self.pathname = pathname


class WMATagger(MutagenTagger):

    """Handles tagging of WMA files"""

    primary_data = ("Title", "Author")
    secondaries = ("WM/AlbumTitle", "WM/AlbumArtist", "WM/Year", "WM/Genre")

    def save_tag(self):
        """Updates the tag with the GUI data."""

        tag = self.tag
        tb = self.tag_frame.tb

        for key in self.text_set:
            try:
                del tag[key]
            except KeyError:
                pass

        for each in self.primary_line:
            val = each[1].get_text().strip()
            if val:
                tag[each[0]] = val
            else:
                try:
                    del tag[each[0]]
                except KeyError:
                    pass

        lines = tb.get_text(tb.get_start_iter(),
                            tb.get_end_iter()).splitlines()
        for line in lines:
            try:
                key, val = line.split("=", 1)
            except ValueError:
                continue
            else:
                key = key.strip()
                val = val.strip()
                if val:
                    try:
                        tag[key] += [ASFUnicodeAttribute(val.decode("utf-8"))]
                    except (KeyError, AttributeError):
                        try:
                            tag[key] = [
                                ASFUnicodeAttribute(val.decode("utf-8"))]
                        except KeyError:
                            print("Unacceptable key", key)
        tag.save()

    def load_tag(self):
        """(re)Writes the tag data to the GUI."""

        tag = self.tag

        for each in self.primary_line:
            try:
                data = tag[each[0]]
            except KeyError:
                pass
            else:
                each[1].set_text("/".join(str(y) for y in data))

        additional = []

        for key in self.secondaries:
            values = tag.get(key, [ASFUnicodeAttribute("")])
            for val in values:
                additional.append(key + "=" + str(val))

        for key in self.text_set:
            if key not in self.primary_data and key not in self.secondaries:
                values = tag[key]
                for val in values:
                    additional.append(key + "=" + str(val))

        self.tag_frame.tb.set_text("\n".join(additional))

    def __init__(self, pathname):
        MutagenTagger.__init__(self, pathname)
        try:
            self.tag = mutagen.asf.ASF(pathname)
            if not isinstance(self.tag, mutagen.asf.ASF):
                raise mutagen.asf.error
        except mutagen.asf.error:
            print("Not a real wma/asf file apparently.")
            self.tag = None
            return


        row = 0
        self.primary_line = []
        for text, entry in ((x, Gtk.Entry()) for x in self.primary_data):
            self.primary_line.append((text, entry))
            lbl_text = Gtk.Label(label=text, halign=Gtk.Align.END)
            lbl_text.set_hexpand(True)
            entry.set_hexpand(True)
            self.attach(lbl_text, 0, row, 1, 1)
            self.attach_next_to(entry, lbl_text, Gtk.PositionType.RIGHT, 1, 1)
            row = row + 1

        self.tag_frame = FreeTagFrame()
        self.tag_frame.set_border_width(5)
        self.tag_frame.set_vexpand(True)
        self.attach(self.tag_frame, 0, row, 2, 1)
        self.tag_frame.show()

        self.text_set = []
        self.show_all()

        for key, val in self.tag.items():
            if key not in self.primary_line and all(isinstance(v, (
                    ASFUnicodeAttribute, str)) for v in val):
                self.text_set.append(key)


class ID3Tagger(MutagenTagger):

    """ID3 tagging with Mutagen."""

    primary_data = (("TIT2", _('title')), ("TPE1", _('artist')),
                    ("TALB", _('album')), ("TRCK", _('track/total')),
                    ("TCON", _('genre')), ("TDRC", _('record date')))

    def save_tag(self):
        """Updates the tag with the GUI data."""

        tag = self.tag

        # Remove all text tags.
        for fid in tag.keys():
            if fid[0] == "T":
                del tag[fid]

        # Add the primary tags.
        for fid, entry in self.primary_line:
            text = entry.get_text().strip()
            if text:
                frame = getattr(id3, fid)
                tag[fid] = frame(3, [text])

        # Add the freeform text tags.
        tb = self.tag_frame.tb
        lines = tb.get_text(tb.get_start_iter(),
                            tb.get_end_iter()).splitlines()

        for line in lines:
            try:
                fid, val = line.split(":", 1)

            except ValueError:
                continue

            fid = fid.strip()
            val = val.strip().decode("utf-8")

            try:
                frame = id3.Frames[fid]
            except NameError:
                continue

            if not issubclass(frame, id3.TextFrame):
                continue

            if frame is id3.TXXX:
                try:
                    key, val = val.split("=", 1)

                except ValueError:
                    continue

                f = frame(3, key.strip(), [val.strip()])
                tag[f.HashKey] = f

            else:
                try:
                    val_list = tag[fid].text
                except KeyError:
                    tag[fid] = frame(3, [val])
                else:
                    val_list.append(val)

        tag.save()

    def load_tag(self):
        """(re)Writes the tag data to the GUI."""

        additional = []
        done = []

        for fid, entry in self.primary_line:
            try:
                frame = self.tag[fid]
                if fid[0] == "T":
                    try:
                        entry.set_text(frame.text[0])
                    except TypeError:
                        # Handle occurrence of ID3Timestamp.
                        entry.set_text(str(frame.text[0]))
                    for each in frame.text[1:]:
                        additional.append(fid + ":" + each)
            except KeyError:
                entry.set_text("")

            done.append(fid)

        for fid, frame in self.tag.items():
            if fid[0] == "T" and fid not in done:
                sep = "=" if fid.startswith("TXXX:") else ":"
                for text in frame.text:
                    additional.append(fid + sep + text)

        self.tag_frame.tb.set_text("\n".join(additional))

    def __init__(self, pathname, force=False):
        MutagenTagger.__init__(self, pathname)
        if force:
            try:
                self.tag = mutagen.File(pathname)
                if not isinstance(self.tag, MP3):
                    raise mutagen.mp3.error
            except mutagen.mp3.error:
                print("Not a real mp3 file apparently.")
                self.tag = None
                return
            try:
                self.tag.add_tags()
                print("Added ID3 tags to", pathname)
            except mutagen.id3.error:
                print("Existing ID3 tags found.")
        else:
            try:
                # Obtain ID3 tags from a non mp3 file.
                self.tag = mutagen.id3.ID3(pathname)
            except mutagen.id3.error:
                self.tag = None
                return

        self.primary_line = []
        row = 0
        for frame, text, entry in (
                (x, y, Gtk.Entry()) for x, y in self.primary_data):
            self.primary_line.append((frame, entry))
            lbl_frame = Gtk.Label(label=frame)
            lbl_text = Gtk.Label(label=text, halign=Gtk.Align.END)
            lbl_text.set_hexpand(True)
            entry.set_hexpand(True)
            self.attach(lbl_frame, 0, row, 1, 1)
            self.attach_next_to(lbl_text, lbl_frame, Gtk.PositionType.RIGHT, 1, 1)
            self.attach_next_to(entry, lbl_text, Gtk.PositionType.RIGHT, 1, 1)
            row = row + 1

        self.tag_frame = FreeTagFrame()
        set_tip(self.tag_frame, _('Add any other ID3 text frames here.\ne.g. '
                                  'TIT2:Alternate Title\nThis will be appended onto the main TIT2 tag.'
                                  '\n\nEnter user defined text frames like this:\nTXXX:foo=bar\n\n'
                                  'For more information visit www.id3.org.'))
        self.tag_frame.set_border_width(5)
        self.tag_frame.set_vexpand(True)
        # TC: Remaining textual ID3 data is show below this heading.
        self.tag_frame.set_label(_(' Additional Text Frames '))
        self.attach(self.tag_frame, 0, row, 3, 1)
        self.show_all()


class MP4Tagger(MutagenTagger):

    """MP4 tagging with Mutagen."""

    primary_data = (("\xa9nam", _('Title')), ("\xa9ART", _('Artist')),
                    ("\xa9alb", _('Album')), ("trkn", _('Track')),
                    ("\xa9gen", _('Genre')), ("\xa9day", _('Year')))

    def save_tag(self):
        """Updates the tag with the GUI data."""

        tag = self.tag
        for fid, entry in self.primary_line:
            text = entry.get_text().strip()
            if fid == "trkn":
                mo1 = re.search("\d+", text)
                try:
                    track = int(text[mo1.start():mo1.end()])
                except AttributeError:
                    new_val = None
                else:
                    text = text[mo1.end():]
                    mo2 = re.search("\d+", text)
                    try:
                        total = int(text[mo2.start():mo2.end()])
                    except AttributeError:
                        new_val = [(track, 0)]
                    else:
                        new_val = [(track, total)]
            else:
                new_val = [text] if text else None

            if new_val is not None:
                tag[fid] = new_val
            else:
                try:
                    del tag[fid]
                except KeyError:
                    pass

        tag.save()

    def load_tag(self):
        """(re)Writes the tag data to the GUI."""

        additional = []

        for fid, entry in self.primary_line:
            try:
                frame = self.tag[fid][0]
            except KeyError:
                entry.set_text("")
            else:
                if fid == "trkn":
                    if frame[1]:
                        entry.set_text("%d/%d" % frame)
                    else:
                        entry.set_text(str(frame[0]))
                else:
                    entry.set_text(frame)

    def __init__(self, pathname):
        MutagenTagger.__init__(self, pathname)
        try:
            self.tag = mutagen.mp4.MP4(pathname)
            if not isinstance(self.tag, mutagen.mp4.MP4):
                raise mutagen.mp4.error
        except mutagen.mp4.error:
            print("Not a real mp4 file apparently.")
            self.tag = None
            return

        self.set_hexpand(True)
        self.primary_line = []
        row = 0
        for frame, text, entry in (
                (x, y, Gtk.Entry()) for x, y in self.primary_data):
            self.primary_line.append((frame, entry))
            lbl_text = Gtk.Label(label=text, halign=Gtk.Align.END)
            lbl_text.set_hexpand(True)
            entry.set_hexpand(True)
            self.attach(lbl_text, 0, row, 1, 1)
            self.attach_next_to(entry, lbl_text, Gtk.PositionType.RIGHT, 1, 1)
            row = row + 1
        self.show_all()

class NativeTagger(MutagenTagger):

    """Native format tagging with Mutagen. Mostly FLAC and Ogg."""

    blacklist = "coverart", "metadata_block_picture"

    def save_tag(self):
        """Updates the tag with the GUI data."""

        tag = self.tag

        for key in tag.keys():
            if key not in self.blacklist:
                del tag[key]

        tb = self.tag_frame.tb
        lines = tb.get_text(tb.get_start_iter(),
                            tb.get_end_iter()).splitlines()

        for line in lines:
            try:
                key, val = line.split("=", 1)
            except ValueError:
                continue
            else:
                key = key.strip()
                val = val.strip()
                if key not in self.blacklist and val:
                    try:
                        tag[key] += [val.decode("utf-8")]
                    except (KeyError, AttributeError):
                        try:
                            tag[key] = [val.decode("utf-8")]
                        except KeyError:
                            print("Unacceptable key", key)

        tag.save()

    def load_tag(self):
        """(re)Writes the tag data to the GUI."""

        tag = self.tag
        lines = []
        primaries = "title", "artist", "author", "album",\
            "tracknumber", "tracktotal", "genre", "date"

        for key in primaries:
            try:
                values = tag[key]
            except KeyError:
                lines.append(key + "=")
            else:
                for val in values:
                    lines.append(key + "=" + val)

        for key, values in tag.items():
            if key not in primaries and key not in self.blacklist:
                for val in values:
                    lines.append(key + "=" + val)

        self.tag_frame.tb.set_text("\n".join(lines))

    def __init__(self, pathname, ext):
        MutagenTagger.__init__(self, pathname)
        self.tag = mutagen.File(pathname)
        if isinstance(self.tag, (MP3, APEv2)):
            # MP3 and APEv2 have their own specialised tagger.
            self.tag = None
            return

        self.tag_frame = FreeTagFrame()
        self.add(self.tag_frame)
        self.tag_frame.set_hexpand(True)
        self.tag_frame.set_vexpand(True)
        self.tag_frame.show()


class ApeTagger(MutagenTagger):

    """APEv2 tagging with Mutagen."""

    opener = {"ape": MonkeysAudio, "mpc": Musepack}

    def save_tag(self):
        """Updates the tag with the GUI data."""

        tag = self.tag

        for key, values in tag.items():
            if isinstance(values, APETextValue):
                del tag[key]

        tb = self.tag_frame.tb
        lines = tb.get_text(tb.get_start_iter(),
                            tb.get_end_iter()).splitlines()

        for line in lines:
            try:
                key, val = line.split("=", 1)
            except ValueError:
                continue
            else:
                key = key.strip()
                val = val.strip()
                if val:
                    try:
                        tag[key].value += "\0" + val.decode()
                    except (KeyError, AttributeError):
                        try:
                            tag[key] = APETextValue(val.decode(), 0)
                        except KeyError:
                            print("Unacceptable key", key)

        tag.save()

    def load_tag(self):
        """(re)Writes the tag data to the GUI."""

        tag = self.tag
        lines = []
        primaries = "TITLE", "ARTIST", "AUTHOR", "ALBUM",\
            "TRACKNUMBER", "TRACKTOTAL", "GENRE", "DATE"

        for key in primaries:
            try:
                values = tag[key]
            except KeyError:
                lines.append(key + "=")
            else:
                for val in values:
                    lines.append(key + "=" + val)

        for key, values in tag.items():
            if key not in primaries and isinstance(values, APETextValue):
                for val in values:
                    lines.append(key + "=" + val)

        self.tag_frame.tb.set_text("\n".join(lines))

    def __init__(self, pathname, extension):
        MutagenTagger.__init__(self, pathname)

        try:
            self.tag = self.opener[extension](pathname)
        except KeyError:
            try:
                self.tag = APEv2(pathname)
            except:
                print("ape tag not found")
                self.tag = None
                return
            else:
                print("ape tag found on non-native format")
        except:
            print("failed to create tagger for native format")
            self.tag = None
            return
        else:
            try:
                self.tag.add_tags()
            except:
                print("ape tag found on native format")
            else:
                print("no existing ape tags found")

        self.tag_frame = FreeTagFrame()
        self.tag_frame.set_hexpand(True)
        self.tag_frame.set_vexpand(True)
        self.add(self.tag_frame)
        self.tag_frame.show()


class MutagenGUI:
    ext2name = {
        "aac": "AAC", "mp3": "ID3", "mp2": "ID3", "mp4": "MP4", "m4a": "MP4", "spx": "Speex",
        "flac": "FLAC", "ogg": "Ogg Vorbis", "oga": "XIPH Ogg audio", "opus": "Ogg Opus",
        "m4b": "MP4", "m4p": "MP4", "wma": "Windows Media Audio"}

    def destroy_and_quit(self, widget, data=None):
        Gtk.main_quit()
        sys.exit(0)

    def update_playlists(self, pathname, idjcroot):
        newplaylistdata = idjcroot.player_left.get_media_metadata(pathname)
        idjcroot.player_left.update_playlist(newplaylistdata)
        idjcroot.player_right.update_playlist(newplaylistdata)

    @staticmethod
    def is_supported(pathname):
        supported = ["mp2", "mp3", "ogg", "oga"]
        if FGlobs.avenabled:
            supported += ["aac", "mp4", "m4a",
                          "m4b", "m4p", "ape", "mpc", "wma"]
        if FGlobs.flacenabled:
            supported.append("flac")
        if FGlobs.speexenabled:
            supported.append("spx")
        if FGlobs.opusenabled:
            supported.append("opus")
        extension = os.path.splitext(pathname)[1][1:].lower()
        if supported.count(extension) != 1:
            if extension:
                print("File type", extension, "is not supported for tagging")
            return False
        else:
            return extension

    def __init__(self, pathname, encoding, idjcroot=None):
        if not pathname:
            print("Tagger not supplied any pathname.")
            return

        extension = self.is_supported(pathname)
        if extension == False:
            print("Tagger file extension", extension, "not supported.")
            return

        self.window = Gtk.Window(Gtk.WindowType.TOPLEVEL)
        if idjcroot is not None:
            idjcroot.window_group.add_window(self.window)
        self.window.set_size_request(550, 450)
        # TC: Window title.
        self.window.set_title(_('IDJC Tagger') + pm.title_extra)
        self.window.set_destroy_with_parent(True)
        self.window.set_border_width(9)
        self.window.set_resizable(True)
        if idjcroot == None:
            self.window.connect("destroy", self.destroy_and_quit)
        grid = Gtk.Grid()
        self.window.add(grid)
        grid.show()
        label = Gtk.Label()
        if idjcroot:
            if encoding is not None:
                label.set_markup("<b>" + _('Filename:') +
                                 " " + GLib.markup_escape_text(str(os.path.split(
                                     pathname)[1], encoding)) + "</b>")
            else:
                label.set_markup("<b>" + _('Filename:') +
                                 " " + GLib.markup_escape_text(os.path.split(
                                     pathname)[1]) + "</b>")
        else:
            label.set_markup("<b>" + _('Filename:') + " " +
                             GLib.markup_escape_text(str(os.path.split(
                                 pathname)[1], "latin1")) + "</b>")
        grid.add(label)
        grid.set_column_homogeneous(True)
        label.show()

        apply_button = Gtk.Button.new_with_label("_Apply")
        if idjcroot is not None:
            apply_button.connect_object_after("clicked", self.update_playlists,
                                              pathname, idjcroot)
        notebook = Gtk.Notebook()
        notebook.set_border_width(2)
        notebook.set_vexpand(True)
        grid.attach_next_to(notebook, label, Gtk.PositionType.BOTTOM, 3, 1)
        notebook.show()

        reload_button = Gtk.Button.new_with_label("_Revert")
        grid.attach_next_to(reload_button, notebook, Gtk.PositionType.BOTTOM, 1, 1)
        reload_button.show()

        close_button = Gtk.Button.new_with_label("_Close")
        close_button.connect_object("clicked", Gtk.Window.destroy, self.window)
        grid.attach_next_to(close_button, reload_button, Gtk.PositionType.RIGHT, 1, 1)
        close_button.show()

        grid.attach_next_to(apply_button, close_button, Gtk.PositionType.RIGHT, 1, 1)
        apply_button.show()
        grid.insert_next_to(reload_button, Gtk.PositionType.RIGHT)


        try:
            self.ape = ApeTagger(pathname, extension)

            if extension in ("mp3", "aac"):
                self.id3 = ID3Tagger(pathname, True)
                self.native = None
            else:
                self.id3 = ID3Tagger(pathname, False)
                if extension in ("mp4", "m4a", "m4b", "m4p"):
                    self.native = MP4Tagger(pathname)
                elif extension == "wma":
                    self.native = WMATagger(pathname)
                elif extension in ("ape", "mpc"):
                    # APE tags are native to this format.
                    self.native = None
                else:
                    self.native = NativeTagger(pathname, ext=extension)

            if self.id3 is not None and self.id3.tag is not None:
                reload_button.connect("clicked", lambda x: self.id3.load_tag())
                apply_button.connect("clicked", lambda x: self.id3.save_tag())
                label = Gtk.Label(label="ID3")
                notebook.append_page(self.id3, label)
                self.id3.show()

            if self.ape is not None and self.ape.tag is not None:
                reload_button.connect("clicked", lambda x: self.ape.load_tag())
                apply_button.connect("clicked", lambda x: self.ape.save_tag())
                label = Gtk.Label(label="APE v2")
                notebook.append_page(self.ape, label)
                self.ape.show()

            if self.native is not None and self.native.tag is not None:
                reload_button.connect("clicked",
                                      lambda x: self.native.load_tag())
                apply_button.connect("clicked",
                                     lambda x: self.native.save_tag())
                label = Gtk.Label(label=_('Native') + " (" + self.ext2name[
                    extension] + ")")
                notebook.append_page(self.native, label)
                self.native.show()

            reload_button.clicked()

            apply_button.connect_object_after("clicked",
                                              Gtk.Window.destroy, self.window)
            self.window.show()
        except IOError as e:
            print(e)
            self.window.destroy()
