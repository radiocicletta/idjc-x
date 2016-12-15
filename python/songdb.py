"""Music database connectivity and display."""

#   Copyright (C) 2012 Stephen Fairchild (s-fairchild@users.sourceforge.net)
#             (C) 2012 Brian Millham (bmillham@users.sourceforge.net)
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

from __future__ import print_function

import os
import ntpath
import time
import types
import gettext
import threading
import json
from functools import partial, wraps
from collections import deque, defaultdict
from contextlib import contextmanager
from urllib import quote

import glib
import gobject
import pango
import gtk
try:
    import MySQLdb as sql
except ImportError:
    have_songdb = False
else:
    have_songdb = True

from idjc import FGlobs
from .tooltips import set_tip
from .gtkstuff import threadslock, gdklock, DefaultEntry, NotebookSR
from .gtkstuff import idle_add, timeout_add, source_remove


__all__ = ['MediaPane', 'have_songdb']

AMPACHE = "Ampache"
AMPACHE_3_7 = "Ampache 3.7"
PROKYON_3 = "Prokyon 3"
FUZZY, CLEAN, WHERE, DIRTY = xrange(4)

t = gettext.translation(FGlobs.package_name, FGlobs.localedir, fallback=True)
_ = t.gettext
N_ = lambda t: t


def dirname(pathname):
    if pathname.startswith("/") and not pathname.startswith("//"):
        return os.path.dirname(pathname)
    return ntpath.dirname(pathname)

def basename(pathname):
    if pathname.startswith("/") and not pathname.startswith("//"):
        return os.path.basename(pathname)
    return ntpath.basename(pathname)


def thread_only(func):
    """Guard a method from being called from outside the thread context."""
    
    @wraps(func)
    def inner(self, *args, **kwargs):
        assert threading.current_thread() == self
        func(self, *args, **kwargs)
    return inner


class DBAccessor(threading.Thread):
    """A class to hide the intricacies of database access.
    
    When the database connection is dropped due to timeout it will silently 
    remake the connection and continue on with its work.
    """
    
    def __init__(self, hostnameport, user, password, database, notify):
        """The notify function must lock gtk before accessing widgets."""
        
        threading.Thread.__init__(self)
        try:
            hostname, port = hostnameport.rsplit(":", 1)
            port = int(port)
        except ValueError:
            hostname = hostnameport
            port = 3306  # MySQL uses this as the default port.

        self.hostname = hostname
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.notify = notify
        self._handle = None  # No connections made until there is a query.
        self._cursor = None
        self.jobs = deque()
        self.semaphore = threading.Semaphore()
        self.keepalive = True
        self.start()

    def request(self, sql_query, handler, failhandler=None):
        """Add a request to the job queue.
        
        The failhandler may "raise exception" to reconnect and try again or
        it may return...
            False, None: to run the handler
            True: to cancel the job
        """
        
        self.jobs.append((sql_query, handler, failhandler))
        self.semaphore.release()

    def close(self):
        """Clean up the worker thread prior to disposal."""
        
        if self.is_alive():
            self.keepalive = False
            self.semaphore.release()
            return

    def run(self):
        """This is the worker thread."""

        notify = partial(idle_add, threadslock(self.notify))
        
        try:
            while self.keepalive:
                self.semaphore.acquire()
                if self.keepalive and self.jobs:
                    query, handler, failhandler = self.jobs.popleft()

                    trycount = 0
                    while trycount < 3:
                        try:
                            try:
                                rows = self._cursor.execute(*query)
                            except sql.Error as e:
                                if failhandler is not None:
                                    if failhandler(e, notify):
                                        break
                                    rows = 0
                                else:
                                    raise e
                        except (sql.Error, AttributeError) as e:
                            if not self.keepalive:
                                return
                            
                            if isinstance(e, sql.OperationalError):
                                # Unhandled errors will be treated like
                                # connection failures.
                                try:
                                    self._cursor.close()
                                except Exception:
                                    pass
                                    
                                try:
                                    self._handle.close()
                                except Exception:
                                    pass
                                
                            if not self.keepalive:
                                return

                            notify(_('Connecting'))
                            trycount += 1
                            try:
                                self._handle = sql.Connection(
                                    host=self.hostname, port=self.port,
                                    user=self.user, passwd=self.password,
                                    db=self.database, connect_timeout=6,
                                    charset='utf8',
                                    compress=True)
                                self._cursor = self._handle.cursor()
                            except sql.Error as e:
                                notify(_("Connection failed (try %d)") %
                                                                    trycount)
                                print(e)
                                time.sleep(0.5)
                            else:
                                # This causes problems if other
                                # processes try to access the database,
                                # so set autocommit to 1
                                try:
                                    self._handle.autocommit(True)
                                except sql.MySQLError:
                                    notify(_('Connected: autocommit mode failed'))
                                else:
                                    notify(_('Connected: autocommit mode set'))
                            notify(_('Connected'))
                        else:
                            if not self.keepalive:
                                return
                            handler(self, self.request, self._cursor, notify,
                                                                        rows)
                            break
                    else:
                        notify(_('Job dropped'))
        finally:
            try:
                self._cursor.close()
            except Exception:
                pass
            try:
                self._handle.close()
            except Exception:
                pass
            notify(_('Disconnected'))

    @thread_only
    def purge_job_queue(self, remain=0):
        while len(self.jobs) > remain:
            self.jobs.popleft()
            self.semaphore.acquire()

    @thread_only
    def disconnect(self):
        try:
            self._handle.close()
        except sql.Error:
            idle_add(threadslock(self.notify), _('Problem dropping connection'))
        else:
            idle_add(threadslock(self.notify), _('Connection dropped'))

    @thread_only
    def replace_cursor(self, cursor):
        """Handler may break off the cursor to pass along its data."""
        
        assert cursor is self._cursor
        self._cursor = self._handle.cursor()


class UseSettings(dict):
    """Holder of data generated while using the database.
    
    It's for storage of data like the preferred browse view, catalog selection.
    """
    
    def __init__(self, key_controls):
        self._key_controls = key_controls
        self._hide_top = True
        dict.__init__(self)

    @contextmanager
    def _toplayer(self):
        self._hide_top = False
        yield
        self._hide_top = True

    def _get_top_level_key(self):
        """The currently active key.
        
        When the database is activated the 'Settings' user interface is locked
        so this key is guaranteed to not change during that time.
        """
        
        return " ".join(s.get_text().replace(" ", "") for s in
                                                            self._key_controls)
        
    def __getitem__(self, key):
        if self._hide_top:
            tlk = self._get_top_level_key()
            return dict.__getitem__(self, tlk)[key]
        else:
            return super(UseSettings, self).__getitem__(key)

    def __setitem__(self, key, value):
        if self._hide_top:
            tlk = self._get_top_level_key()
            try:
                dict_ = dict.__getitem__(self, tlk)
            except:
                dict_ = {}
                dict.__setitem__(self, tlk, dict_)
            
            dict_[key] = value
        else:
            super(UseSettings, self).__setitem__(key, value)

    def get_text(self):
        with self._toplayer():
            save_data = json.dumps(self)
            return save_data
        
    def set_text(self, data):
        with self._toplayer():
            try:
                data = json.loads(data)
            except ValueError:
                pass
            else:
                self.update(data)


class Settings(gtk.Table):
    """Connection details widgets."""
    
    def __init__(self, name):
        self._name = name
        gtk.Table.__init__(self, 5, 4)
        self.set_border_width(10)
        self.set_row_spacings(1)
        for col, spc in zip(xrange(3), (3, 10, 3)):
            self.set_col_spacing(col, spc)

        self._controls = []
        self.textdict = {}

        # Attachment for labels.
        l_attach = partial(self.attach, xoptions=gtk.SHRINK | gtk.FILL)
        
        # Top row.
        hostportlabel, self.hostnameport = self._factory(
            _('Hostname[:Port]'), 'localhost', "hostnameport")
        l_attach(hostportlabel, 0, 1, 0, 1)
        self.attach(self.hostnameport, 1, 4, 0, 1)
        
        # Second row.
        userlabel, self.user = self._factory(_('User Name'), "ampache", "user")
        l_attach(userlabel, 0, 1, 2, 3)
        self.attach(self.user, 1, 2, 2, 3)
        dblabel, self.database = self._factory(_('Database'), "ampache",
                                                                    "database")
        l_attach(dblabel, 2, 3, 2, 3)
        self.attach(self.database, 3, 4, 2, 3)

        self.usesettings = UseSettings(self._controls[:])
        self.textdict["songdb_usesettings_" + name] = self.usesettings
        
        # Third row.
        passlabel, self.password = self._factory(_('Password'), "", "password")
        self.password.set_visibility(False)
        l_attach(passlabel, 0, 1, 3, 4)
        self.attach(self.password, 1, 2, 3, 4)
        

    def get_data(self):
        """Collate parameters for DBAccessor contructors."""
        
        accdata = {}
        for key in "hostnameport user password database".split():
            accdata[key] = getattr(self, key).get_text().strip()

        return accdata, self.usesettings

    def set_sensitive(self, sens):
        """Just specific contents of the table are made insensitive."""

        for each in self._controls:
            each.set_sensitive(sens)

    def _factory(self, labeltext, entrytext, control_name):
        """Widget factory method."""

        label = gtk.Label(labeltext)
        label.set_alignment(1.0, 0.5)

        if entrytext:
            entry = DefaultEntry(entrytext, True)
        else:
            entry = gtk.Entry()

        entry.set_size_request(10, -1)
        self._controls.append(entry)
        self.textdict["songdb_%s_%s" % (control_name, self._name)] = entry
        
        return label, entry


class PrefsControls(gtk.Frame):
    """Database controls as visible in the preferences window."""
    
    def __init__(self):
        gtk.Frame.__init__(self)
        self.set_border_width(3)
        label = gtk.Label(" %s " % 
                            _('Prokyon3 or Ampache (song title) Database'))
        set_tip(label, _('You can make certain media databases accessible in '
                            'IDJC for easy drag and drop into the playlists.'))
        self.set_label_widget(label)
        vbox = gtk.VBox()
        vbox.set_border_width(6)
        vbox.set_spacing(2)
        self.add(vbox)
        
        self._notebook = NotebookSR()
        if have_songdb:
            vbox.pack_start(self._notebook, False)

        self._settings = []
        for i in range(1, 5):
            settings = Settings(str(i))
            self._settings.append(settings)
            label = gtk.Label(str(i))
            self._notebook.append_page(settings, label)

        self.dbtoggle = gtk.ToggleButton(_('Music Database'))
        self.dbtoggle.connect("toggled", self._cb_dbtoggle)

        hbox = gtk.HBox()
        hbox.set_spacing(2)
        
        self._disconnect = gtk.Button()
        self._disconnect.set_sensitive(False)
        image = gtk.image_new_from_stock(gtk.STOCK_DISCONNECT, gtk.ICON_SIZE_MENU)
        self._disconnect.add(image)
        self._disconnect.connect("clicked", lambda w: self.dbtoggle.set_active(False))
        hbox.pack_start(self._disconnect, False)
        
        self._connect = gtk.Button()
        image = gtk.image_new_from_stock(gtk.STOCK_CONNECT, gtk.ICON_SIZE_MENU)
        self._connect.add(image)
        self._connect.connect("clicked", lambda w: self.dbtoggle.set_active(True))
        hbox.pack_start(self._connect, False)
        
        self._statusbar = gtk.Statusbar()
        self._statusbar.set_has_resize_grip(False)
        cid = self._statusbar.get_context_id("all output")
        self._statusbar.push(cid, _('Disconnected'))
        hbox.pack_start(self._statusbar)

        if have_songdb:
            vbox.pack_start(hbox, False)
        else:
            vbox.set_sensitive(False)
            label = gtk.Label(_('Module mysql-python (MySQLdb) required'))
            vbox.add(label)

        self.show_all()
        
        # Save and Restore.
        self.activedict = {"songdb_active": self.dbtoggle,
                            "songdb_page": self._notebook}
        self.textdict = {}
        for each in self._settings:
            self.textdict.update(each.textdict)

    def credentials(self):
        if self.dbtoggle.get_active():
            active = self._notebook.get_current_page()
        else:
            active = None

        pages = []
        for i, settings in enumerate(self._notebook.get_children()):
            creddict = settings.get_data()[0]
            creddict.update({"active": i == active})
            pages.append(creddict)
        
        return pages

    def disconnect(self):
        self.dbtoggle.set_active(False)    
        
    def bind(self, callback):
        """Connect with the activate method of the view pane."""
        
        self.dbtoggle.connect("toggled", self._cb_bind, callback)

    def _cb_bind(self, widget, callback):
        """This runs when the database is toggled on and off."""
        
        if widget.get_active():
            settings = self._notebook.get_nth_page(
                                            self._notebook.get_current_page())
            accdata, usesettings = settings.get_data()
            accdata["notify"] = self._notify
        else:
            accdata = usesettings = None

        callback(accdata, usesettings)

    def _cb_dbtoggle(self, widget):
        """Parameter widgets to be made insensitive when db is active."""
    
        if widget.get_active():
            self._connect.set_sensitive(False)
            self._disconnect.set_sensitive(True)
            settings = self._notebook.get_nth_page(
                                            self._notebook.get_current_page())
            for settings_page in self._settings:
                if settings_page is settings:
                    settings_page.set_sensitive(False)
                else:
                    settings_page.hide()
        else:
            self._connect.set_sensitive(True)
            self._disconnect.set_sensitive(False)
            for settings_page in self._settings:
                settings_page.set_sensitive(True)
                settings_page.show()

    def _notify(self, message):
        """Display status messages beneath the prefs settings."""
        
        print("Song title database:", message)
        cid = self._statusbar.get_context_id("all output")
        self._statusbar.pop(cid)
        self._statusbar.push(cid, message)
        # To ensure readability of long messages also set the tooltip.
        self._statusbar.set_tooltip_text(message)


class PageCommon(gtk.VBox):
    """Base class for all pages."""
    
    def __init__(self, notebook, label_text, controls):
        gtk.VBox.__init__(self)
        self.set_spacing(2)
        self.scrolled_window = gtk.ScrolledWindow()
        self.scrolled_window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_ALWAYS)
        self.pack_start(self.scrolled_window)
        self.tree_view = gtk.TreeView()
        self.tree_view.set_enable_search(False)
        self.tree_selection = self.tree_view.get_selection()
        self.scrolled_window.add(self.tree_view)
        self.pack_start(controls, False)
        label = gtk.Label(label_text)
        notebook.append_page(self, label)
        self._update_id = deque()
        self._acc = None

    @property
    def db_type(self):
        return self._db_type

    def get_col_widths(self):
        return ",".join([str(x.get_width() or x.get_fixed_width())
                                                    for x in self.tree_cols])

    def in_text_entry(self):
        return False

    def set_col_widths(self, data):
        """Restore column width values. Includes a data validity check."""

        if len(self.tree_cols) == data.count(",") + 1:
            for width, col in zip(data.split(","), self.tree_cols):
                if width != "0":
                    col.set_fixed_width(int(width))
        else:
            print("can't restore column widths")

    def activate(self, accessor, db_type, usesettings):
        self._acc = accessor
        self._db_type = db_type
        self._usesettings = usesettings

    def deactivate(self):
        while self._update_id:
            context, namespace = self._update_id.popleft()
            namespace[0] = True
            source_remove(context)
        
        self._acc = None
        model = self.tree_view.get_model()
        self.tree_view.set_model(None)
        if model is not None:
            model.clear()
            
    def repair_focusability(self):
        self.tree_view.set_flags(gtk.CAN_FOCUS)

    @staticmethod
    def _make_tv_columns(tree_view, parameters):
        """Build a TreeViewColumn list from a table of data."""

        list_ = []
        for p in parameters:
            try:
                # Check if there is an extra parameter to set the renderer
                label, data_index, data_function, mw, el, renderer = p
            except:
                label, data_index, data_function, mw, el = p
                renderer = gtk.CellRendererText()
                renderer.props.ellipsize = el
            column = gtk.TreeViewColumn(label, renderer)
            if mw != -1:
                column.set_resizable(True)
                column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
                column.set_min_width(mw)
                column.set_fixed_width(mw + 50)
            tree_view.append_column(column)
            list_.append(column)
            if data_function is not None:
                column.set_cell_data_func(renderer, data_function, data_index)
            else:
                column.add_attribute(renderer, 'text', data_index)

        return list_

    def _handler(self, acc, request, cursor, notify, rows):
        # Lock against the very start of the update functions.
        with gdklock():
            while self._update_id:
                context, namespace = self._update_id.popleft()
                source_remove(context)
                # Idle functions to receive the following and know to clean-up.
                namespace[0] = True

        try:
            self._old_cursor.close()
        except sql.Error as e:
            print(str(e))
        except AttributeError:
            pass

        self._old_cursor = cursor
        acc.replace_cursor(cursor)
        # Scrap intermediate jobs whose output would merely slow down the
        # user interface responsiveness.
        namespace = [False, ()]
        context = idle_add(self._update_1, acc, cursor, rows, namespace)
        self._update_id.append((context, namespace))

class ViewerCommon(PageCommon):
    """Base class for TreePage and FlatPage."""
    
    def __init__(self, notebook, label_text, controls, catalogs):
        self.catalogs = catalogs
        self.notebook = notebook
        self._reload_upon_catalogs_changed(enable_notebook_reload=True)
        PageCommon.__init__(self, notebook, label_text, controls)
        self.tree_view.enable_model_drag_source(gtk.gdk.BUTTON1_MASK,
            self._sourcetargets, gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_COPY)
        self.tree_view.connect_after("drag-begin", self._cb_drag_begin)
        self.tree_view.connect("drag-data-get", self._cb_drag_data_get)

    def deactivate(self):
        self._reload_upon_catalogs_changed()
        super(ViewerCommon, self).deactivate()

    def _reload_upon_catalogs_changed(self, enable_notebook_reload=False):
        handler_id = []
        handler_id.append(self.catalogs.connect("changed",
            self._on_catalogs_changed, enable_notebook_reload, handler_id))
        self._old_cat_data = None

    def _on_catalogs_changed(self, widget, enable_notebook_reload, handler_id):
        self.catalogs.disconnect(handler_id[0])  # Only run once.
        if enable_notebook_reload:
            self.notebook.connect("switch-page", self._on_page_change)
        self.reload()

    def _on_page_change(self, notebook, page, page_num):
        if notebook.get_nth_page(page_num) == self:
            self.reload()

    _sourcetargets = (  # Drag and drop source target specs.
        ('text/plain', 0, 1),
        ('TEXT', 0, 2),
        ('STRING', 0, 3))

    def _cb_drag_begin(self, widget, context):
        """Set icon for drag and drop operation."""

        context.set_icon_stock(gtk.STOCK_CDROM, -5, -5)

    def _cb_drag_data_get(self, tree_view, context, selection, target, etime):
        model, paths = self.tree_selection.get_selected_rows()
        data = []
        for catalog, pathname in self._drag_data(model, paths):
            valid, pathname = self.catalogs.transform_path(catalog, pathname)
            if valid:
                data.append("file://" + pathname)
        selection.set(selection.target, 8, "\n".join(data))

    def _cond_cell_secs_to_h_m_s(self, column, renderer, model, iter, cell):
        if model.get_value(iter, 0) >= 0:
            return self._cell_secs_to_h_m_s(column, renderer, model, iter, cell)
        else:
            renderer.set_property("text", "")
    
    def _cell_k(self, column, renderer, model, iter, cell):
        bitrate = model.get_value(iter, cell)
        if bitrate == 0:
            renderer.set_property("text", "")
        elif self._db_type == "P3":
            renderer.set_property("text", "%dk" % bitrate)
        elif bitrate > 9999 and self._db_type in (AMPACHE, AMPACHE_3_7):
            renderer.set_property("text", "%dk" % (bitrate // 1000))
        renderer.set_property("xalign", 1.0)

    def _query_cook_common(self, query):
        if self._db_type == AMPACHE:
            query = query.replace("__played_by_me__", "'1' as played_by_me")
        else:
            query = query.replace("__played_by_me__", """SUBSTR(MAX(CONCAT(object_count.date, IF(ISNULL(agent), NULL,
                        IF(STRCMP(LEFT(agent,5), "IDJC:"), 2,
                        IF(STRCMP(agent, "IDJC:1"), 0, 1))))), 11) AS played_by_me""")
        return query.replace("__catalogs__", self.catalogs.sql())

    def _cell_show_unknown(self, column, renderer, model, iter, data):
        text, max_lastplay_date, played_by, played, played_by_me, cat = model.get(iter, *data)
        if text is None: text = _('<unknown>')
        weight = pango.WEIGHT_NORMAL
        if not played:
            col = 'black'
            renderer.props.background_set = False
        else:
            value, percent, weight = self._get_played_percent(cat, max_lastplay_date)
            col, bg_col = ViewerCommon._set_color(played_by_me, percent)
            renderer.props.background_set = True
            renderer.props.background = bg_col
        renderer.props.text = text
        renderer.props.foreground = col
        renderer.props.weight = weight

    def _cell_show_nested(self, column, renderer, model, iter, data):
        text, max_lastplay_date, played_by, played, played_by_me, cat = model.get(iter, *data)
        if text is None: text = _('<unknown>')
        col = "black"
        weight = pango.WEIGHT_NORMAL
        renderer.props.background_set = False
        if model.iter_depth(iter) == 0:
            col = "red"
        elif played:
            value, percent, weight = self._get_played_percent(cat, max_lastplay_date)
            col, bg_col = ViewerCommon._set_color(played_by_me, percent)
            renderer.props.background_set = True
            renderer.props.background = bg_col
        renderer.props.text = text
        renderer.props.foreground = col
        renderer.props.weight = weight

    def _cell_progress(self, column, renderer, model, iter, data):
        max_lastplay_date, played_by, played, cat= model.get(iter, *data)
        if not played:
            text = _("Not Played")
            value = 0
            renderer.props.visible = False
        else:
            value, percent, weight = self._get_played_percent(cat, max_lastplay_date)
            text = ViewerCommon._format_lastplay_date(max_lastplay_date)
            text += "(" + (played_by or _('<unknown>')) + ")"
            renderer.props.visible = True
        renderer.props.text = text
        renderer.props.value = value

    @staticmethod
    def _cell_pathname(column, renderer, model, iter, data, partition, transform):
        catalog, text = model.get(iter, *data)
        if text:
            present, text = transform(catalog, text)
            renderer.props.foreground = "black" if present else "red"
            text = partition(text)

        renderer.props.text = text
        
    def _cell_path(self, *args, **kwargs):
        kwargs["partition"] = dirname
        kwargs["transform"] = self.catalogs.transform_path
        self._cell_pathname(*args, **kwargs) 
        
    def _cell_filename(self, *args, **kwargs):
        kwargs["partition"] = basename
        kwargs["transform"] = lambda c, p: (True, p)
        self._cell_pathname(*args, **kwargs)
    
    @staticmethod
    def _cell_secs_to_h_m_s(column, renderer, model, iter, cell):
        v_in = model.get_value(iter, cell)
        d, h, m, s = ViewerCommon._secs_to_h_m_s(v_in)
        if d:
            v_out = "%dd:%02d:%02d" % (d, h, m)
        else:
            if h:
                v_out = "%d:%02d:%02d" % (h, m, s)
            else:
                v_out = "%d:%02d" % (m, s)
        renderer.set_property("xalign", 1.0)
        renderer.set_property("text", v_out)
        
    @staticmethod
    def _cell_ralign(column, renderer, model, iter, cell):
        val = model.get_value(iter, cell)
        if val:
            renderer.set_property("xalign", 1.0)
            renderer.set_property("text", val)
        else:
            renderer.set_property("text", "")

    @staticmethod
    def _secs_to_h_m_s(value):
        m, s = divmod(value, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        return d, h, m, s

    @staticmethod
    def _format_lastplay_date(value):
        if value is None:
            return _("<unknown>") + " "
        difftime = time.time() - int(value)
        d, h, m, s = ViewerCommon._secs_to_h_m_s(difftime)
        return "%dd %dh %dm ago " % (d, h, m)

    def _get_played_percent(self, catalog, value):
        if value is None:
            return 0, 0.0, pango.WEIGHT_NORMAL + 50
        now = time.time()
        max_days_ago = self.catalogs.lpscale(catalog)
        diff = now - int(value)
        if diff > max_days_ago:
            value = 0
            percent = 0.35
            weight = pango.WEIGHT_NORMAL + 100
        else:
            percent = 1.0 - (float(diff) / float(max_days_ago))
            value = 100 * percent
            # Refactor percent used for colors to be .4 to 1.0, as anything
            # below about .35 starts to look to much like black.
            percent = (percent * .6) + .4
            # Get a weight from 500 to 900
            weight = ((percent * .4) * 1000) + pango.WEIGHT_NORMAL + 100
        return value, percent, weight

    @staticmethod
    def _set_color(text, percent=1.0):
        #print("text: ", text)
        if percent == 1.0:
            bg_col = "white"
        elif int(text) == 1:
            bg_col = "Powder Blue"
        else:
            bg_col = "Light Pink"
        return (gtk.gdk.color_from_hsv(0.0, 1.0, percent),
                gtk.gdk.color_from_hsv(0.6666, 1.0, percent),
                gtk.gdk.color_from_hsv(0.3333, 1.0, percent))[int(text)], bg_col

class ExpandAllButton(gtk.Button):
    def __init__(self, expanded, tooltip=None):
        expander = gtk.Expander()
        expander.set_expanded(expanded)
        expander.show_all()
        gtk.Button.__init__(self)
        self.add(expander)
        if tooltip is not None:
            set_tip(self, tooltip)


class TreePage(ViewerCommon):
    """Browsable UI with tree structure."""

    # *depth*(0), *treecol*(1), album(2), album_prefix(3), year(4), disk(5),
    # album_id(6), tracknumber(7), title(8), artist(9), artist_prefix(10),
    # pathname(11), bitrate(12), length(13), catalog_id(14), max_date_played(15),
    # played_by(16), played(17), played_by_me(18)
    # The order chosen negates the need for a custom sort comparison function.
    DATA_SIGNATURE = int, str, str, str, int,\
                     int, int, int, str, str, str, str,\
                     int, int, int, str, str, int, str
    BLANK_ROW = tuple(x() for x in DATA_SIGNATURE[2:])

    def __init__(self, notebook, catalogs):
        self.controls = gtk.HBox()
        layout_store = gtk.ListStore(str, gtk.TreeStore, gobject.TYPE_PYOBJECT)
        self.layout_combo = gtk.ComboBox(layout_store)
        cell_text = gtk.CellRendererText()
        self.layout_combo.pack_start(cell_text)
        self.layout_combo.add_attribute(cell_text, "text", 0)
        self.controls.pack_start(self.layout_combo, False)
        self.right_controls = gtk.HBox()
        self.right_controls.set_spacing(1)
        self.tree_rebuild = gtk.Button()
        set_tip(self.tree_rebuild, _('Reload the database.'))
        image = gtk.image_new_from_stock(gtk.STOCK_REFRESH, gtk.ICON_SIZE_MENU)
        self.tree_rebuild.add(image)
        self.tree_rebuild.connect("clicked", self._cb_tree_rebuild)
        self.tree_rebuild.set_use_stock(True)
        tree_expand = ExpandAllButton(True, _('Expand entire tree.'))
        tree_collapse = ExpandAllButton(False, _('Collapse tree.'))
        sg = gtk.SizeGroup(gtk.SIZE_GROUP_HORIZONTAL)
        for each in (self.tree_rebuild, tree_expand, tree_collapse):
            self.right_controls.pack_start(each, False)
            sg.add_widget(each)
        self.controls.pack_end(self.right_controls, False)

        ViewerCommon.__init__(self, notebook, _('Browse'), self.controls,
                                                                    catalogs)
        
        self.tree_view.set_enable_tree_lines(True)
        tree_expand.connect_object("clicked", gtk.TreeView.expand_all,
                                                                self.tree_view)
        tree_collapse.connect_object("clicked", gtk.TreeView.collapse_all,
                                                                self.tree_view)
        self.tree_cols = self._make_tv_columns(self.tree_view, (
                ("", (1, 15, 16, 17, 18, 14), self._cell_show_nested, 180, pango.ELLIPSIZE_END),
                # TC: Track artist.
                (_('Artist'), (10, 9), self._data_merge, 100, pango.ELLIPSIZE_END),
                # TC: The disk number of the album track.
                (_('Disk'), 5, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
                # TC: The album track number.
                (_('Track'), 7, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
                # TC: Track playback time.
                (_('Duration'), 13, self._cond_cell_secs_to_h_m_s, -1, pango.ELLIPSIZE_NONE),
                (_('Last Played'), (15, 16, 17, 14), self._cell_progress, -1, None, gtk.CellRendererProgress()),
                (_('Bitrate'), 12, self._cell_k, -1, pango.ELLIPSIZE_NONE),
                (_('Filename'), (14, 11), self._cell_filename, 100, pango.ELLIPSIZE_END),
                # TC: Directory path to a file.
                (_('Path'), (14, 11), self._cell_path, -1, pango.ELLIPSIZE_NONE),
                ))

        self.artist_store = gtk.TreeStore(*self.DATA_SIGNATURE)
        self.album_store = gtk.TreeStore(*self.DATA_SIGNATURE)
        layout_store.append((_('Artist - Album - Title'), self.artist_store, (1, )))
        layout_store.append((_('Album - [Disk] - Title'), self.album_store, (2, )))
        self.layout_combo.set_active(0)
        self.layout_combo.connect("changed", self._cb_layout_combo)

        self.loading_vbox = gtk.VBox()
        self.loading_vbox.set_border_width(20)
        self.loading_vbox.set_spacing(20)
        # TC: The database tree view is being built (populated).
        self.loading_label = gtk.Label()
        self.loading_vbox.pack_start(self.loading_label, False)
        self.progress_bar = gtk.ProgressBar()
        self.loading_vbox.pack_start(self.progress_bar, False)
        self.pack_start(self.loading_vbox)
        self._pulse_id = deque()
        
        self.show_all()

    def set_loading_view(self, loading):
        if loading:
            self.progress_bar.set_fraction(0.0)
            self.loading_label.set_text(_('Fetching'))
            self.controls.hide()
            self.scrolled_window.hide()
            self.loading_vbox.show()
        else:
            self.layout_combo.emit("changed")
            self.loading_vbox.hide()
            self.scrolled_window.show()
            self.controls.show()

    def activate(self, *args, **kwargs):
        PageCommon.activate(self, *args, **kwargs)
        try:
            layout_mode = self._usesettings["layout mode"]
        except KeyError:
            pass
        else:
            self.layout_combo.set_active(layout_mode)

    def deactivate(self):
        while self._pulse_id:
            source_remove(self._pulse_id.popleft())
        self.progress_bar.set_fraction(0.0)
        super(TreePage, self).deactivate()

    def reload(self):
        if self.catalogs.update_required(self._old_cat_data):
            self.tree_rebuild.clicked()

    def _cb_layout_combo(self, widget):
        iter = widget.get_active_iter()
        store, hide = widget.get_model().get(iter, 1, 2)
        self.tree_view.set_model(store)
        for i, col in enumerate(self.tree_cols):
            col.set_visible(i not in hide)
        self._usesettings["layout mode"] = widget.get_active()

    def _cb_tree_rebuild(self, widget):
        """(Re)load the tree with info from the database."""

        self._old_cat_data = self.catalogs.copy_data()
        self.set_loading_view(True)
        if self._db_type == PROKYON_3:
            query = """SELECT
                    album,
                    "" as alb_prefix,
                    IFNULL(albums.year, 0) as year,
                    0 as disk,
                    IFNULL(albums.id, 0) as album_id,
                    tracknumber,
                    title,
                    tracks.artist as artist,
                    "" as art_prefix,
                    CONCAT_WS('/',path,filename) as file,
                    bitrate, length,
                    0 as catalog_id,
                    0 as max_date_played,
                    "" as played_by,
                    0 as played,
                    0 as played_by_me
                    FROM tracks
                    LEFT JOIN albums on tracks.album = albums.name
                     AND tracks.artist = albums.artist
                    ORDER BY tracks.artist, album, tracknumber, title"""
        elif self._db_type in (AMPACHE, AMPACHE_3_7):
            query = """SELECT
                    album.name as album,
                    album.prefix as alb_prefix,
                    album.year as year,
                    album.disk as disk,
                    song.album as album_id,
                    track as tracknumber,
                    title,
                    artist.name as artist,
                    artist.prefix as art_prefix,
                    file,
                    bitrate,
                    time as length,
                    catalog.id as catalog_id,
                    MAX(object_count.date) as max_date_played,
                    SUBSTR(MAX(CONCAT(object_count.date, user.fullname)), 11) AS played_by,
                    played,
                    __played_by_me__
                    FROM song
                    LEFT JOIN artist ON song.artist = artist.id
                    LEFT JOIN album ON song.album = album.id
                    LEFT JOIN object_count ON song.id = object_count.object_id
                                AND object_count.object_type = "song"
                    LEFT JOIN user ON user.id = object_count.user
                    LEFT JOIN catalog ON song.catalog = catalog.id
                    WHERE __catalogs__
                    GROUP BY song.id
                    ORDER BY artist.name, album, disk, tracknumber, title"""
                    
            query = self._query_cook_common(query)
        else:
            print("unsupported database type:", self._db_type)
            return
            
        self._pulse_id.append(timeout_add(1000, self._progress_pulse))
        self._acc.request((query,), self._handler, self._failhandler)

    def _drag_data(self, model, path):
        iter = model.get_iter(path[0])
        for each in self._more_drag_data(model, iter):
            yield each 
                
    def _more_drag_data(self, model, iter):
        depth, catalog, pathname = model.get(iter, 0, 14, 11)
        if depth == 0:
            yield catalog, pathname
        else:
            iter = model.iter_children(iter)
            while iter is not None:
                for each in self._more_drag_data(model, iter):
                    yield each
            
                iter = model.iter_next(iter)

    @threadslock
    def _progress_pulse(self):
        self.progress_bar.pulse()
        return True

    def _data_merge(self, column, renderer, model, iter, elements):
        renderer.props.text = self._join(*model.get(iter, *elements))

    @staticmethod
    def _join(prefix, name):
        if prefix and name:
            return prefix + " " + name
        return prefix or name or ""

    ###########################################################################

    def _handler(self, acc, request, cursor, notify, rows):
        PageCommon._handler(self, acc, request, cursor, notify, rows)
        acc.disconnect()

    def _failhandler(self, exception, notify):
        if isinstance(exception, sql.InterfaceError):
            raise exception  # Recover.
        
        print(exception)
        
        notify(_('Tree fetch failed'))
        idle_add(threadslock(self.loading_label.set_text), _('Fetch Failed!'))
        while self._pulse_id:
            source_remove(self._pulse_id.popleft())
        
        return True  # Drop job. Don't run handler.

    ###########################################################################

    @threadslock
    def _update_1(self, acc, cursor, rows, namespace):
        if namespace[0]:
            return False
            
        self.loading_label.set_text(_('Populating'))
        # Turn off progress bar pulser.
        while self._pulse_id:
            source_remove(self._pulse_id.popleft())

        # Clean away old data.
        self.tree_view.set_model(None)
        self.artist_store.clear()
        self.album_store.clear()

        namespace = [False, (0.0, None, None, None, {}, None, None, None, None)]
        do_max = min(max(30, rows / 100), 200)  # Data size to process.
        total = 2.0 * rows
        context = idle_add(self._update_2, acc, cursor, total, do_max,
                                                            [], namespace)
        self._update_id.append((context, namespace))
        return False

    @threadslock
    def _update_2(self, acc, cursor, total, do_max, store, namespace):
        kill, (done, iter_l, iter_1, iter_2, letter, artist, album, art_prefix, alb_prefix) = namespace
        if kill:
            return False

        r_append = self.artist_store.append
        l_append = store.append
        BLANK_ROW = self.BLANK_ROW

        rows = cursor.fetchmany(do_max)
        if not rows:
            store.sort()
            namespace = [False, (done, ) + (None, ) * 11]
            context = idle_add(self._update_3, acc, total, do_max,
                                                        store, namespace)
            self._update_id.append((context, namespace))
            return False

        for row in rows:
            if acc.keepalive == False:
                return False

            l_append(row)
            try:
                art_letter = row[7].decode('utf-8')[0].upper()
            except IndexError:
                art_letter = ""

            if art_letter in letter:
                iter_l = letter[art_letter]
            else:
                iter_l = letter[art_letter] = r_append(None, (-1, art_letter) + BLANK_ROW)
            if album == row[0] and artist == row[7] and \
                                alb_prefix == row[1] and art_prefix == row[8]:
                iter_3 = r_append(iter_2, (0, row[6]) + row)
                continue
            else:
                if artist != row[7] or art_prefix != row[8]:
                    artist = row[7]
                    art_prefix = row[8]
                    iter_1 = r_append(iter_l, (-2, self._join(art_prefix, artist)) + BLANK_ROW)
                    album = None
                if album != row[0] or alb_prefix != row[1]:
                    album = row[0]
                    alb_prefix = row[1]
                    year = row[2]
                    if year:
                        albumtext = "%s (%d)" % (self._join(alb_prefix, album), year)
                    else:
                        albumtext = album
                    iter_2 = r_append(iter_1, (-3, albumtext) + BLANK_ROW)
                iter_3 = r_append(iter_2, (0, row[6]) + row)
                
        done += do_max
        self.progress_bar.set_fraction(sorted((0.0, done / total, 1.0))[1])
        namespace[1] = done, iter_l, iter_1, iter_2, letter, artist, album, art_prefix, alb_prefix
        return True

    @threadslock
    def _update_3(self, acc, total, do_max, store, namespace):
        kill, (done, iter_l, iter_1, iter_2, letter, artist, album, art_prefix, alb_prefix, year, disk, album_id) = namespace
        if kill:
            return False

        append = self.album_store.append
        pop = store.pop
        BLANK_ROW = self.BLANK_ROW
        if letter is None: letter = {}
        
        for each in xrange(do_max):
            if acc.keepalive == False:
                return False
                
            try:
                row = pop(0)
            except IndexError:
                self.set_loading_view(False)
                return False

            try:
                alb_letter = row[0].decode('utf-8')[0].upper()
            except IndexError:
                alb_letter = ""
        
            if alb_letter in letter:
                iter_l = letter[alb_letter]
            else:
                iter_l = letter[alb_letter] = append(None, (-1, alb_letter) + BLANK_ROW)
            if album_id == row[4]:
                iter_3 = append(iter_2, (0, row[6]) + row)
                continue
            else:
                if album != row[0] or year != row[2] or alb_prefix != row[1]:
                    album = row[0]
                    alb_prefix = row[1]
                    year = row[2]
                    disk = None
                    if year:
                        albumtext = "%s (%d)" % (self._join(alb_prefix, album), year)
                    else:
                        albumtext = album
                    iter_1 = append(iter_l, (-2, albumtext) + BLANK_ROW)
                if disk != row[3]:
                    disk = row[3]
                    if disk == 0:
                        iter_2 = iter_1
                    else:
                        iter_2 = append(iter_1, (-3, _('Disk %d') % disk)
                                                                + BLANK_ROW)
                iter_3 = append(iter_2, (0, row[6]) + row)

        done += do_max
        self.progress_bar.set_fraction(min(done / total, 1.0))
        namespace[1] = done, iter_l, iter_1, iter_2, letter, artist, album, art_prefix, alb_prefix, year, disk, album_id
        return True


class FlatPage(ViewerCommon):
    """Flat list based user interface with a search facility."""
    
    def __init__(self, notebook, catalogs):
        # Base class overwrites these values.
        self.scrolled_window = self.tree_view = self.tree_selection = None
        self.transfrom = self.db_accessor = None

        # TC: User specified search filter entry box title text.
        self.controls = gtk.Frame(" %s " % _('Filters'))
        self.controls.set_shadow_type(gtk.SHADOW_OUT)
        self.controls.set_border_width(1)
        self.controls.set_label_align(0.5, 0.5)
        filter_vbox = gtk.VBox()
        filter_vbox.set_border_width(3)
        filter_vbox.set_spacing(1)
        self.controls.add(filter_vbox)
        
        fuzzy_hbox = gtk.HBox()
        filter_vbox.pack_start(fuzzy_hbox, False)
        # TC: A type of search on any data field matching paritial strings.
        fuzzy_label = gtk.Label(_('Fuzzy Search'))
        fuzzy_hbox.pack_start(fuzzy_label, False)
        self.fuzzy_entry = gtk.Entry()
        self.fuzzy_entry.connect("changed", self._cb_fuzzysearch_changed)
        fuzzy_hbox.pack_start(self.fuzzy_entry, True, True, 0)
        
        where_hbox = gtk.HBox()
        filter_vbox.pack_start(where_hbox, False)
        # TC: WHERE is an SQL keyword.
        where_label = gtk.Label(_('WHERE'))
        where_hbox.pack_start(where_label, False)
        self.where_entry = gtk.Entry()
        self.where_entry.connect("activate", self._cb_update)
        where_hbox.pack_start(self.where_entry)
        image = gtk.image_new_from_stock(gtk.STOCK_EXECUTE,
                                                        gtk.ICON_SIZE_BUTTON)
        self.update_button = gtk.Button()
        self.update_button.connect("clicked", self._cb_update)
        self.update_button.set_image(image)
        image.show
        where_hbox.pack_start(self.update_button, False)
        
        ViewerCommon.__init__(self, notebook, _("Search"), self.controls,
                                                                    catalogs)
 
        # Row data specification:
        # index(0), ARTIST(1), ALBUM(2), TRACKNUM(3), TITLE(4), DURATION(5), BITRATE(6),
        # pathname(7), disk(8), catalog_id(9), max_date_played(10),
        # played_by(11), played(12), played_by_me(13)
        self.list_store = gtk.ListStore(
                            int, str, str, int, str, int, int,
                            str, int, int, str,
                            str, int, str)
        self.tree_cols = self._make_tv_columns(self.tree_view, (
            ("(0)", 0, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
            (_('Artist'), (1, 10, 11, 12, 13, 9), self._cell_show_unknown, 100, pango.ELLIPSIZE_END),
            (_('Album'), (2, 10, 11, 12, 13, 9), self._cell_show_unknown, 100, pango.ELLIPSIZE_END),
            (_('Title'), (4, 10, 11, 12, 13, 9), self._cell_show_unknown, 100, pango.ELLIPSIZE_END),
            (_('Last Played'), (10, 11, 12, 9), self._cell_progress, -1, None, gtk.CellRendererProgress()),
            (_('Disk'), 8, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
            (_('Track'), 3, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
            (_('Duration'), 5, self._cell_secs_to_h_m_s, -1, pango.ELLIPSIZE_NONE),
            (_('Bitrate'), 6, self._cell_k, -1, pango.ELLIPSIZE_NONE),
            (_('Filename'), (9, 7), self._cell_filename, 100, pango.ELLIPSIZE_END),
            (_('Path'), (9, 7), self._cell_path, -1, pango.ELLIPSIZE_NONE),
            ))

        self.tree_view.set_rules_hint(True)
        self.tree_view.set_rubber_banding(True)
        self.tree_selection.set_mode(gtk.SELECTION_MULTIPLE)

    def reload(self):
        if self.catalogs.update_required(self._old_cat_data):
            self.update_button.clicked()

    def in_text_entry(self):
        return any(x.has_focus() for x in (self.fuzzy_entry, self.where_entry))

    def deactivate(self):
        self.fuzzy_entry.set_text("")
        self.where_entry.set_text("")
        super(FlatPage, self).deactivate()

    def repair_focusability(self):
        PageCommon.repair_focusability(self)
        self.fuzzy_entry.set_flags(gtk.CAN_FOCUS)
        self.where_entry.set_flags(gtk.CAN_FOCUS)

    _queries_table = {
        PROKYON_3:
            {FUZZY: (CLEAN, """
                    SELECT artist,album,tracknumber,title,length,bitrate,
                    CONCAT_WS('/',path,filename) as file,
                    0 as disk, 0 as catalog_id,
                    0 as max_date_played,
                    "" as played_by,
                    0 as played,
                    0 as played_by_me
                    FROM tracks
                    WHERE MATCH (artist,album,title,filename) AGAINST (%s)
                    """),
        
            WHERE: (DIRTY, """
                    SELECT artist,album,tracknumber,title,length,bitrate,
                    CONCAT_WS('/',path,filename) as file,
                    0 as disk, 0 as catalog_id,
                    0 as max_date_played,
                    "" as played_by,
                    0 as played,
                    0 as played_by_me
                    FROM tracks WHERE (%s)
                    ORDER BY artist,album,path,tracknumber,title
                    """)},
        
        AMPACHE:
            {FUZZY: (CLEAN, """
                    SELECT
                    concat_ws(" ",artist.prefix,artist.name),
                    concat_ws(" ",album.prefix,album.name),
                    track as tracknumber, title, time as length,bitrate,
                    file,
                    album.disk as disk,
                    catalog.id as catalog_id,
                    MAX(object_count.date) as max_date_played,
                    SUBSTR(MAX(CONCAT(object_count.date, user.fullname)), 11) AS played_by,
                    played,
                    __played_by_me__
                    FROM song
                    LEFT JOIN artist ON artist.id = song.artist
                    LEFT JOIN album ON album.id = song.album
                    LEFT JOIN object_count ON song.id = object_count.object_id
                                AND object_count.object_type = "song"
                    LEFT JOIN user ON user.id = object_count.user
                    LEFT JOIN catalog ON song.catalog = catalog.id
                    WHERE
                         (MATCH(album.name) against(%s)
                          OR MATCH(artist.name) against(%s)
                          OR MATCH(title) against(%s)) AND __catalogs__
                    GROUP BY song.id
                    """),

            WHERE: (DIRTY, """
                    SELECT
                    concat_ws(" ", artist.prefix, artist.name) as artist,
                    concat_ws(" ", album.prefix, album.name) as albumname,
                    track as tracknumber, title,time as length, bitrate,
                    file,
                    album.disk as disk,
                    catalog.id as catalog_id,
                    MAX(object_count.date) as max_date_played,
                    SUBSTR(MAX(CONCAT(object_count.date, user.fullname)), 11) AS played_by,
                    played,
                    __played_by_me__
                    FROM song
                    LEFT JOIN album on album.id = song.album
                    LEFT JOIN artist on artist.id = song.artist
                    LEFT JOIN object_count ON song.id = object_count.object_id
                                AND object_count.object_type = "song"
                    LEFT JOIN user ON user.id = object_count.user
                    LEFT JOIN catalog ON song.catalog = catalog.id
                    WHERE (%s) AND __catalogs__
                    GROUP BY song.id
                    ORDER BY
                    artist.name, album.name, file, album.disk, track, title
                    """)}
    }
    _queries_table[AMPACHE_3_7] = _queries_table[AMPACHE]

    def _cb_update(self, widget):
        self._old_cat_data = self.catalogs.copy_data()
        try:
            table = self._queries_table[self._db_type]
        except KeyError:
            print("unsupported database type")
            return

        user_text = self.fuzzy_entry.get_text().strip()
        if user_text:
            access_mode, query = table[FUZZY]
        else:
            access_mode, query = table[WHERE]
            user_text = self.where_entry.get_text().strip()
            if not user_text:
                self.where_entry.set_text("")
                while self._update_id:
                    context, namespace = self._update_id.popleft()
                    source_remove(context)
                    namespace[0] = True
                self.list_store.clear()
                return

        query = self._query_cook_common(query)
        qty = query.count("(%s)")
        if access_mode == CLEAN:
            query = (query, (user_text,) * qty)
        elif access_mode == DIRTY:  # Accepting of SQL code in user data.
            query = (query % ((user_text,) * qty),)
        else:
            print("unknown database access mode", access_mode)
            return

        self._acc.request(query, self._handler, self._failhandler)
        return

    @staticmethod
    def _drag_data(model, paths):
        """Generate tuples of (catalog, pathname) for the given paths."""
        
        for path in paths:
            row = model[path]
            yield row[9], row[7]

    def _cb_fuzzysearch_changed(self, widget):
        if widget.get_text().strip():
            self.where_entry.set_sensitive(False)
            self.where_entry.set_text("")
        else:
            self.where_entry.set_sensitive(True)
        self.update_button.clicked()
        
    ###########################################################################

    def _handler(self, acc, *args, **kwargs):
        PageCommon._handler(self, acc, *args, **kwargs)
        acc.purge_job_queue(1)

    def _failhandler(self, exception, notify):
        notify(str(exception))
        if exception[0] == 2006:
            raise

        idle_add(self.tree_view.set_model, None)
        idle_add(self.list_store.clear)

    ###########################################################################
    
    @threadslock
    def _update_1(self, acc, cursor, rows, namespace):
        if not namespace[0]:
            self.tree_view.set_model(None)
            self.list_store.clear()
            namespace[1] = (0, )  # found = 0
            context = idle_add(self._update_2, acc, cursor, namespace)
            self._update_id.append((context, namespace))
        return False

    @threadslock
    def _update_2(self, acc, cursor, namespace):
        kill, (found, ) = namespace
        if kill:
            return False
        
        next_row = cursor.fetchone
        append = self.list_store.append

        for i in xrange(100):
            if acc.keepalive == False:
                return False

            try:
                row = next_row()
            except sql.Error:
                return False

            if row:
                found += 1
                append((found, ) + row)
            else:
                if found:
                    self.tree_cols[0].set_title("(%s)" % found)
                    self.tree_view.set_model(self.list_store)
                return False

        namespace[1] = (found, )
        return True


class CatalogsInterface(gobject.GObject):
    __gsignals__ = { "changed" : (gobject.SIGNAL_RUN_LAST, None, ()) }
    time_unit_table = {N_('Minutes'): 60, N_('Hours'): 3600,
                       N_('Days'): 86400, N_('Weeks'): 604800}
    
    def __init__(self):
        gobject.GObject.__init__(self)
        self._dict = {}

    def clear(self):
        self._dict.clear()
        
    def copy_data(self):
        return self._dict.copy()
    
    def update(self, liststore):
        """Replacement of the standard dict update method.
        
        This one interprets a CatalogPage gtk.ListStore.
        """
        
        self._dict.clear()
        for row in liststore:
            if row[0]:
                self._dict[row[5]] = {
                    "peel" : row[1], "prepend" : row[2],
                    "lpscale" : self._lpscale_calc(row[3], row[4]),
                    "name" : row[6], "path" : row[7], "last_update" : row[8],
                    "last_clean" : row[9], "last_add" : row[10]
                }
                
        self.emit("changed")

    @classmethod
    def _lpscale_calc(cls, qty, unit):
        return qty * cls.time_unit_table[unit]

    def transform_path(self, catalog, path):
        if len(path) < 4:
            return False, path  # Path too short to be valid.

        # Conversion of Windows paths to a Unix equivalent.
        if path[:2] in ("\\\\", "//"):
            # Handle UNC paths. Throw away the server and share parts.
            try:
                path = ntpath.splitunc(path)[1].replace("\\", "/")
            except Exception:
                return False, path
        elif path[0] != '/':
            # Assume it's a regular Windows path and try to convert it.
            path = '/' + path.replace('\\', '/')

        peel = self._dict[catalog]["peel"]
        if peel > 0:
            path = "/" + path.split("/", peel + 1)[-1]

        path = os.path.normpath(self._dict[catalog]["prepend"] + path)
        return os.path.isfile(path), path
    
    def sql(self):
        ids = tuple(x for x in self._dict.iterkeys())
        if not ids:
            return "FALSE"

        if len(ids) == 1:
            which = "catalog = %d" % ids[0]
        else:
            which = "catalog IN %s" % str(ids)

        return which + ' AND catalog.catalog_type = "local"'
    
    def update_required(self, other):
        if other is None:
            return True

        return self._stripped_copy(self._dict) != self._stripped_copy(other)

    def lpscale(self, catalog):
        return self._dict[catalog]["lpscale"]

    @staticmethod
    def _stripped_copy(_dict):
        copy = {}
        for key1, val1 in _dict.iteritems():
            copy[key1] = {}
            for key2, val2 in val1.iteritems():
                if key2 not in ("peel", "prepend", "lpscale"):
                    copy[key1][key2] = val2

        return copy
    

class CatalogsPage(PageCommon):
    def __init__(self, notebook, interface):
        self.interface = interface
        self.refresh = gtk.Button(stock=gtk.STOCK_REFRESH)
        self.refresh.connect("clicked", self._on_refresh)
        PageCommon.__init__(self, notebook, _("Catalogs"), self.refresh)
        
        # active, peel, prepend, lpscale_qty, lpscale_unit, id, name, path,
        # last_update, last_clean, last_add
        self.list_store = gtk.ListStore(
                        int, int, str, int, str, int, str, str, int, int, int)
        self.tree_cols = self._make_tv_columns(self.tree_view, (
            (_('Name'), 6, None, 65, pango.ELLIPSIZE_END),
            (_('Catalog Path'), 7, None, 100, pango.ELLIPSIZE_END),
            (_('Prepend Path'), 2, None, -1, pango.ELLIPSIZE_NONE)
            ))

        rend1 = gtk.CellRendererToggle()
        rend1.set_activatable(True)
        rend1.connect("toggled", self._on_toggle)
        self.tree_view.insert_column_with_attributes(0, "", rend1, active=0)

        col = gtk.TreeViewColumn(_('Last Played Scale'))

        adj = gtk.Adjustment(0.0, 0.0, 999.0, 1.0, 1.0)
        rend2 = gtk.CellRendererSpin()
        rend2.props.editable = True
        rend2.props.adjustment = adj
        rend2.props.xalign = 1.0
        rend2.connect("editing-started", self._on_spin_editing_started, 3)
        rend2.connect("edited", self._on_spin_edited, 3)
        col.pack_start(rend2, False)
        col.add_attribute(rend2, "text", 3)

        lp_unit_scale_store = gtk.ListStore(str)
        for each in (N_('Minutes'), N_('Hours'), N_('Days'), N_('Weeks')):
            lp_unit_scale_store.append((each,))
        lp_unit_scale_cr = gtk.CellRendererCombo()
        lp_unit_scale_cr.props.has_entry = False
        lp_unit_scale_cr.props.editable = True
        lp_unit_scale_cr.props.model = lp_unit_scale_store
        lp_unit_scale_cr.props.text_column = 0
        lp_unit_scale_cr.connect("changed", self._on_lp_unit_changed)
        col.pack_start(lp_unit_scale_cr, False)
        col.set_cell_data_func(lp_unit_scale_cr, self._translate_scale)
        self.tree_view.insert_column(col, 3)

        adj = gtk.Adjustment(0.0, 0.0, 999.0, 1.0, 1.0)
        rend3 = gtk.CellRendererSpin()
        rend3.props.editable = True
        rend3.props.adjustment = adj
        rend3.props.xalign = 1.0
        rend3.connect("editing-started", self._on_spin_editing_started, 1)
        rend3.connect("edited", self._on_spin_edited, 1)
        col = self.tree_view.insert_column_with_attributes(4, _("Path Peel"),
                                                                rend3, text=1)

        rend4 = self.tree_view.get_column(5).get_cell_renderers()[0]
        rend4.props.editable = True
        rend4.connect("edited", self._on_prepend_edited)

        for rend in (rend3, rend4):
            rend.connect("editing-started", self._on_editing_started)
            rend.connect("editing-canceled", self._on_editing_cancelled)

        self.tree_view.set_rules_hint(True)
        self._block_key_bindings = False

    def in_text_entry(self):
        return self._block_key_bindings

    def activate(self, *args, **kwargs):
        PageCommon.activate(self, *args, **kwargs)
        self.tree_view.get_column(0).set_visible(self._db_type in (AMPACHE, AMPACHE_3_7))
        self.refresh.clicked()

    def deactivate(self, *args, **kwargs):
        PageCommon.deactivate(self, *args, **kwargs)
        self.interface.clear()

    def _translate_scale(self, col, cell, model, iter):
        cell.props.text = _(model.get_value(iter, 4))

    def _get_active_catalogs(self):
        return tuple(x[3] for x in self.list_store if x[0])
        
    def _store_user_data(self):
        dict_ = {}
        for row in self.list_store:
            dict_[str(row[5])] = (row[0], row[1], row[2], row[3], row[4])
        self._usesettings["catalog_data"] = dict_
        
    def _restore_user_data(self):
        try:
            dict_ = self._usesettings["catalog_data"]
        except:
            return

        for row in self.list_store:
            try:
                row[0], row[1], row[2], row[3], row[4] = dict_[str(row[5])]
            except KeyError:
                pass
            except ValueError:
                row[0], row[1], row[2] = dict_[str(row[5])]
                row[3], row[4] = 4, N_('Weeks') 

    def _on_toggle(self, renderer, path):
        iter = self.list_store.get_iter(path)
        if iter is not None:
            old_val = self.list_store.get_value(iter, 0)
            self.list_store.set_value(iter, 0, not old_val)
            self._store_user_data()
            self.interface.update(self.list_store)

    def _on_refresh(self, widget):
        if self._db_type in (AMPACHE, AMPACHE_3_7):
            self.refresh.set_sensitive(False)
            self.tree_view.set_model(None)
            if self._db_type == AMPACHE:
                query = """SELECT id, name, path, last_update, IFNULL(last_clean,0),
                           last_add FROM catalog WHERE enabled=1 ORDER BY name"""
            else:
                query = """SELECT catalog.id, name, path, last_update, IFNULL(last_clean,0),
                           last_add FROM catalog
                           LEFT JOIN catalog_local on catalog.id = catalog_id
                           AND catalog.catalog_type = "local"
                           WHERE enabled=1 ORDER BY name"""
            self._acc.request((query,), self._handler, self._failhandler)
        
        elif self._db_type == PROKYON_3:
            self.list_store.clear()
            self.tree_view.set_model(self.list_store)
            self.list_store.append((1, 0, "", 0, _('N/A'), 0, _('N/A'), _('N/A'), 0, 0, 0))
            self._restore_user_data()
            self.interface.update(self.list_store)

    def _on_editing_started(self, rend, editable, path):
        self._block_key_bindings = True

    def _on_editing_cancelled(self, rend):
        self._block_key_bindings = False

    def _on_spin_editing_started(self, rend, editable, path, index):
        val = self.list_store[path][index]
        rend.props.adjustment.props.value = val

    def _on_spin_edited(self, rend, path, new_data, index):
        self._block_key_bindings = False
        row = self.list_store[path]
        try:
            val = int(new_data.strip() or 0)
        except ValueError:
            pass
        else:
            if val >= 0 and val != row[index]:
                row[index] = min(val, int(rend.props.adjustment.props.upper))
                self._store_user_data()
                self.interface.update(self.list_store)

    def _on_prepend_edited(self, rend, path, new_data):
        self._block_key_bindings = False
        row = self.list_store[path]
        new_data = new_data.strip()
        if new_data != row[2]:
            row[2] = new_data
            self._store_user_data()
            self.interface.update(self.list_store)

    def _on_lp_unit_changed(self, combo, path_string, new_iter):
        text = combo.props.model.get_value(new_iter, 0)
        self.list_store[path_string][4] = text
        self._store_user_data()
        self.interface.update(self.list_store)

    ###########################################################################

    def _failhandler(self, exception, notify):
        notify(str(exception))
        if exception[0] == 2006:
            raise
        
        idle_add(threadslock(self.tree_view.set_model), self.list_store)
        idle_add(threadslock(self.refresh.set_sensitive), True)

    @threadslock
    def _update_1(self, acc, cursor, rows, namespace):
        if not namespace[0]:
            self.list_store.clear()
            
            while 1:
                try:
                    db_row = cursor.fetchone()
                except sql.Error:
                    break

                if db_row is None:
                    break
                
                self.list_store.append((0, 0, "", 4, N_('Weeks')) + db_row)

        self._restore_user_data()
        self.tree_view.set_model(self.list_store)
        self.refresh.set_sensitive(True)
        self.interface.update(self.list_store)
        return False
        

class MediaPane(gtk.VBox):
    """Database song details are displayed in this widget."""

    def __init__(self):
        gtk.VBox.__init__(self)

        self.notebook = gtk.Notebook()
        self.pack_start(self.notebook)
        
        catalogs = CatalogsInterface()
        self._tree_page = TreePage(self.notebook, catalogs)
        self._flat_page = FlatPage(self.notebook, catalogs)
        self._catalogs_page = CatalogsPage(self.notebook, catalogs)
        self.prefs_controls = PrefsControls()

        if have_songdb:
            self.prefs_controls.bind(self._dbtoggle)

        spc = gtk.VBox()
        spc.set_border_width(2)
        self.pack_start(spc, False)
        spc.show()

        self.notebook.show_all()

    def in_text_entry(self):
        if self.get_visible():
            page = self.notebook.get_nth_page(self.notebook.get_current_page())
            return page.in_text_entry()
            
        return False
            
    def repair_focusability(self):
        self._tree_page.repair_focusability()
        self._flat_page.repair_focusability()
        self._catalogs_page.repair_focusability()

    def get_col_widths(self, keyval):
        """Grab column widths as textual data."""
        
        try:
            target = getattr(self, "_%s_page" % keyval)
        except AttributeError as e:
            print(e)
            return ""
        else:
            return target.get_col_widths()
    
    def set_col_widths(self, keyval, data):
        """Column widths are to be restored on application restart."""
        
        if data:
            try:
                target = getattr(self, "_%s_page" % keyval)
            except AttributeError as e:
                print(e)
                return
            else:
                target.set_col_widths(data)

    def _dbtoggle(self, accdata, usesettings):
        if accdata:
            # Connect and discover the database type.
            self.usesettings = usesettings
            for i in range(1, 4):
                setattr(self, "_acc%d" % i, DBAccessor(**accdata))
            self._acc1.request(('SHOW tables',), self._stage_1, self._fail_1)
        else:
            try:
                for i in xrange(1, 4):
                    getattr(self, "_acc%d" % i).close()
            except AttributeError:
                pass
            else:
                for each in "tree flat catalogs".split():
                    getattr(self, "_%s_page" % each).deactivate()
            self.hide()

    @staticmethod
    def schema_test(string, data):
        data = frozenset(x[0] for x in data)
        return frozenset(string.split()).issubset(data)
    
    ###########################################################################

    def _safe_disconnect(self):
        idle_add(threadslock(self.prefs_controls.disconnect))

    def _hand_over(self, db_name):
        self._tree_page.activate(self._acc1, db_name, self.usesettings)
        self._flat_page.activate(self._acc2, db_name, self.usesettings)
        self._catalogs_page.activate(self._acc3, db_name, self.usesettings)
        idle_add(threadslock(self.show))
            
    def _fail_1(self, exception, notify):
        # Give up.
        self._safe_disconnect()
        return True

    def _fail_2(self, exception, notify):
        try:
            code = exception.args[0]
        except IndexError:
            raise

        if code != 1061:
            notify(_('Failed to create FULLTEXT index'))
            print(exception)
            raise

        notify(_('Found existing FULLTEXT index'))

    def _stage_1(self, acc, request, cursor, notify, rows):
        """Running under the accessor worker thread!
        
        Step 1 Identifying database type.
        """
        
        data = cursor.fetchall()
        if self.schema_test("tracks", data):
            request(('DESCRIBE tracks',), self._stage_2, self._fail_1)
        elif self.schema_test("album artist song", data):
            request(('DESCRIBE song',), self._stage_4, self._fail_1)
        else:
            notify(_('Unrecognised database'))
            self._safe_disconnect()
            
    def _stage_2(self, acc, request, cursor, notify, rows):
        """Confirm it's a Prokyon 3 database."""
        
        if self.schema_test("artist title album tracknumber bitrate " 
                                        "path filename", cursor.fetchall()):
            notify(_('Found Prokyon 3 schema'))
            # Try to add a FULLTEXT database.
            request(("""ALTER TABLE tracks ADD FULLTEXT artist (artist,title,
                        album,filename)""",), self._stage_2a, self._fail_2)
        else:
            notify(_('Unrecognised database'))
            self._safe_disconnect()

    def _stage_2a(self, acc, request, cursor, notify, rows):
        request(("ALTER TABLE albums ADD INDEX idjc (name)",),
                self._stage_3, self._fail_2)

    def _stage_3(self, acc, request, cursor, notify, rows):
        self._hand_over(PROKYON_3)

    def _stage_4(self, acc, request, cursor, notify, rows):
        """Test for Ampache database."""

        if self.schema_test("artist title album track bitrate file", 
                                                            cursor.fetchall()):
            request(('DESCRIBE artist',), self._stage_5, self._fail_1)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()

    def _stage_5(self, acc, request, cursor, notify, rows):
        if self.schema_test("name prefix", cursor.fetchall()):
            request(('DESCRIBE artist',), self._stage_6, self._fail_1)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()

    def _stage_6(self, acc, request, cursor, notify, rows):
        if self.schema_test("name prefix", cursor.fetchall()):
            notify('Found Ampache schema')
            request(("ALTER TABLE album ADD FULLTEXT idjc (name)",),
                                                self._stage_7, self._fail_2)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()
        
    def _stage_7(self, acc, request, cursor, notify, rows):
        request(("ALTER TABLE artist ADD FULLTEXT idjc (name)",), self._stage_8,
                                                                self._fail_2)
        
    def _stage_8(self, acc, request, cursor, notify, rows):
        request(("ALTER TABLE song ADD FULLTEXT idjc (title)",), self._stage_9,
                                                                self._fail_2)
    def _stage_9(self, acc, request, cursor, notify, rows):
        notify("Checking ampache type")
        request(("DESCRIBE catalog",), self._stage_10, self._fail_2)

    def _stage_10(self, acc, request, cursor, notify, rows):
        if self.schema_test("path", cursor.fetchall()):
            notify('Found Ampache pre 3.7 schema')
            self._hand_over(AMPACHE)
        else:
            request(("DESCRIBE catalog_local",), self._stage_11, self._fail_2)

    def _stage_11(self, acc, request, cursor, notify, rows):
        if self.schema_test("path", cursor.fetchall()):
            notify('Found Ampache 3.7 schema')
            self._hand_over(AMPACHE_3_7)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()
