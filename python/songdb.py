#   songdb.py: music database connectivity
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


import time
import gettext
import threading
from functools import partial

import gobject
import gtk
try:
    import MySQLdb as sql
except ImportError:
    sql = None

from idjc import FGlobs
from .tooltips import set_tip
from .gtkstuff import threadslock, DefaultEntry


__all__ = ['MediaPane']

t = gettext.translation(FGlobs.package_name, FGlobs.localedir, fallback=True)
_ = t.gettext


class DBAccessor(threading.Thread):
    """A class to hide the intricacies of database access.
    
    When the database connection is dropped due to timeout it will silently 
    remake the connection and continue on with its work.
    """
    
    def __init__(self, hostnameport, user, password, database, notify=lambda m: 0):
        """The notify function must lock gtk before accessing widgets."""
        
        threading.Thread.__init__()
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
        self.handle = None  # No connections made until there is a query.
        self.cursor = None
        self.jobs = []
        self.semaphore = threading.Semaphore()
        self.lock = threading.Lock()
        self.keepalive = True
        start()

    def request(self, sql_query, handler, failhandler):
        """Add a request to the job queue.
        
        sql_query = str()
        def handler(sql cursor): implemented as a generator function with
                                 the yield acting as a cancellation point
                                 if processing a huge data set you want to
                                 allow cancellation once for each artist
        def failhandler(Exception instance)
        """
        
        self.jobs.append((sql_query, handler, failhandler))
        self.semaphore.release()
        
    def run(self):
        while self.keepalive:
            self.notify(_('Ready'))
            self.semaphore.acquire()
            if self.keepalive and self.jobs:
                query, handler, failhandler = self.jobs.pop(0)

                trycount = 0
                while trycount < 4:
                    try:
                        self.cursor.execute(*query)
                    except sql.OperationalError as e:
                        if self.keepalive:
                            # Unhandled errors will be treated like
                            # connection failures.
                            if self.handle.open and failhandler is not None:
                                failhandler(e)
                                break
                            else:
                                try:
                                    self.cursor.close()
                                except Exception:
                                    pass
                                    
                                try:
                                    self.handle.close()
                                except Exception:
                                    pass

                                raise
                        else:
                            break
                    except (sql.Error, AttributeError):
                        with self.lock:
                            if self.keepalive:
                                self.notify(_('Connecting'))
                                trycount += 1
                                try:
                                    self.handle = sql.connect(host=self.hostname,
                                        port=self.port, user=self.user,
                                        passwd=self.password, db=self.database,
                                        connect_timeout=3)
                                    self.cursor = self.handle.cursor()
                                except sql.Error as e:
                                    self.notify(_("Connection failed (try %d)") % i)
                                    print e
                                    time.sleep(0.5)
                                else:
                                    self.notify(_('Connected'))
                    else:
                        if self.keepalive:
                            self.notify(_('Processing'))
                            for dummy in handler(self.cursor):
                                if not self.keepalive:
                                    break
                        break
                
                else:
                    self.notify(_('Job dropped'))
 
        self.notify(_('Disconnected'))

    def close(self):
        """Clean up the worker thread prior to disposal."""
        
        self.keepalive = False
        self.semaphore.release()
        
        # If the thread is stuck on IO unblock it by closing the connection.
        # We should clean up in any event.
        with self.lock:
            try:
                self.cursor.close()
            except Exception:
                pass
                
            try:
                self.handle.close()
            except Exception:
                pass
            
        self.join()  # Hopefully this will complete quickly.
        self.jobs.clear()


class PrefsControls(gtk.Frame):
    """Database controls as visible in the preferences window."""
    
    def __init__(self):
        gtk.Frame.__init__(self)
        self.set_border_width(3)
        label = gtk.Label(" %s " % _('Prokyon3 or Ampache (song title) Database'))
        set_tip(label, _('You can make certain media databases accessible in IDJC for easy drag and drop into the playlists.'))
        self.set_label_widget(label)
        
        self._parameters = []  # List of widgets that should be made insensitive when db is active. 
        if not sql:
            # Feature is disabled.
            vbox = gtk.VBox()
            vbox.set_sensitive(False)
            vbox.set_border_width(3)
            label = gtk.Label(_('Python module MySQLdb required'))
            vbox.add(label)
            self.add(vbox)
            self.data_panel = gtk.VBox()  # Empty placeholder widget.
        else:
            # Control widgets.
            table = gtk.Table(5, 4)
            table.set_border_width(10)
            table.set_row_spacings(1)
            for col, spc in zip(xrange(3), (3, 10, 3)):
                table.set_col_spacing(col, spc)

            # Attachment for labels.
            l_attach = partial(table.attach, xoptions=gtk.SHRINK | gtk.FILL)
            
            # Top row.
            hostportlabel, self._hostport = self._factory(_('Hostname[:Port]'), 'localhost')
            l_attach(hostportlabel, 0, 1, 0, 1)
            table.attach(self._hostport, 1, 4, 0, 1)
            
            # Second row.
            hbox = gtk.HBox()
            hbox.set_spacing(3)
            fpmlabel, self._addchars = self._factory(_('File Path Modify'), None)
            adj = gtk.Adjustment(0.0, 0.0, 999.0, 1.0, 1.0)
            self._delchars = gtk.SpinButton(adj, 0.0, 0)
            self._parameters.append(self._delchars)
            set_tip(self._delchars, _('The number of characters to strip from the left hand side of media file paths.'))
            set_tip(self._addchars, _('The characters to prefix to the media file paths.'))
            l_attach(fpmlabel, 0, 1, 1, 2)
            minus = gtk.Label('-')
            hbox.pack_start(minus, False)
            hbox.pack_start(self._delchars, False)
            plus = gtk.Label('+')
            hbox.pack_start(plus, False)
            hbox.pack_start(self._addchars)
            table.attach(hbox, 1, 4, 1, 2)
            
            # Third row.
            userlabel, self._user = self._factory(_('User Name'), "admin")
            l_attach(userlabel, 0, 1, 3, 4)
            table.attach(self._user, 1, 2, 3, 4)
            dblabel, self._database = self._factory(_('Database'), "ampache")
            l_attach(dblabel, 2, 3, 3, 4)
            table.attach(self._database, 3, 4, 3, 4)
            
            # Fourth row.
            passlabel, self._password = self._factory(_('Password'), 'password')
            self._password.set_visibility(False)
            l_attach(passlabel, 0, 1, 4, 5)
            table.attach(self._password, 1, 2, 4, 5)
            self.dbtoggle = gtk.ToggleButton(_('Music Database'))
            self.dbtoggle.set_size_request(10, -1)
            self.dbtoggle.connect("toggled", self._cb_dbtoggle)
            table.attach(self.dbtoggle, 2, 4, 4, 5)
            
            # Notification row.
            self._statusbar = gtk.Statusbar()
            self._statusbar.set_has_resize_grip(False)
            gtk.gdk.threads_leave()
            self.notify(_('Disconnected'))
            gtk.gdk.threads_enter()
            table.attach(self._statusbar, 0, 4, 5, 6)
            
            self.add(table)
            self.data_panel = gtk.VBox()  # Bring in widget at some point.
            
        self.data_panel.set_no_show_all(False)
        self.show_all()

    @property
    def hostport(self):
        return self._hostport.get_text().strip()
        
    @property
    def user(self):
        return self._user.get_text().strip()
        
    @property
    def password(self):
        return self._password.get_text().strip()
        
    @property
    def database(self):
        return self._database.get_text().strip()
        
    @property
    def delchars(self):
        return self._delchars.get_value()
        
    @property
    def addchars(self):
        return self._addchars.get_text().strip()

    @threadslock
    def notify(self, message):
        """Intended for use by DBAccessor worker thread for status messages."""
        
        self._statusbar.push(1, message)
        self._statusbar.set_tooltip_text(message)  # To show long messages.

    def _cb_dbtoggle(self, widget):
        """Parameter widgets to be made insensitive when db is active."""
    
        sens = not widget.get_active()
    
        for each in self._parameters:
            each.set_sensitive(sens)

    def _factory(self, labeltext, entrytext=None):
        """Widget factory method."""
        
        label = gtk.Label(labeltext)
        label.set_alignment(1.0, 0.5)
        
        if entrytext:
            entry = DefaultEntry(entrytext, True)
        else:
            entry = gtk.Entry()
            
        entry.set_size_request(10, -1)
        self._parameters.append(entry)
        return label, entry


class MediaPane(gtk.Frame):
    """Database song details are displayed in this widget."""

    def __init__(self):
        gtk.Frame.__init__(self)
        self.set_shadow_type(gtk.SHADOW_IN)
        self.set_border_width(6)
        self.set_label_align(0.5, 0.5)
        vbox = gtk.VBox()
        self.add(vbox)
        self.notebook = gtk.Notebook()
        vbox.pack_start(self.notebook, True)
        
        # Tree UI with Artist, Album, Title heirarchy.
        # TC: Refers to the tree view of the tracks database.
        buttonbox = gtk.HButtonBox()
        buttonbox.set_layout(gtk.BUTTONBOX_SPREAD)
        tree_update = gtk.Button(gtk.STOCK_REFRESH)
        #tree_update.connect("clicked", self._cb_tree_update)
        tree_update.set_use_stock(True)
        tree_expand = gtk.Button(_('_Expand'), None, True)
        image = gtk.image_new_from_stock(gtk.STOCK_ADD, gtk.ICON_SIZE_BUTTON)
        tree_expand.set_image(image)
        tree_collapse = gtk.Button(_('_Collapse'), None, True)
        image = gtk.image_new_from_stock(gtk.STOCK_REMOVE, gtk.ICON_SIZE_BUTTON)
        tree_collapse.set_image(image)
        for each in (tree_update, tree_expand, tree_collapse):
            buttonbox.add(each)

        self.treeview, self.treescroll, self.treealt = self._makeview(
                                        self.notebook, _('Tree'), buttonbox)
        self.treeview.set_enable_tree_lines(True)
        self.treeview.set_rubber_banding(True)
        treeselection = self.treeview.get_selection()
        treeselection.set_mode(gtk.SELECTION_MULTIPLE)
        treeselection.set_select_function(self._tree_select_func)
        tree_expand.connect_object("clicked", gtk.TreeView.expand_all,
                                                                self.treeview)
        tree_collapse.connect_object("clicked", gtk.TreeView.collapse_all,
                                                                self.treeview)
        # id, ARTIST-ALBUM-TITLE, TRACK, DURATION, BITRATE, filename, path, disk
        self.treestore = gtk.TreeStore(int, str, int, int, int, str, str, int)
        self.treeview.set_model(self.treestore)
        self.treecols = self._makecolumns(self.treeview, (
                ("%s - %s - %s" % (_('Artist'), _('Album'), _('Title')), 1,
                                                self._cell_show_unknown, 180),
                # TC: The disk number of the album track.
                (_('Disk'), 7, self._cell_ralign, -1),
                # TC: The album track number.
                (_('Track'), 2, self._cell_ralign, -1),
                # TC: Track playback time.
                (_('Duration'), 3, self._cond_cell_secs_to_h_m_s, -1),
                (_('Bitrate'), 4, self._cell_k, -1),
                (_('Filename'), 5, None, 100),
                # TC: Directory path to a file.
                (_('Path'), 6, None, -1),
                ))
        
        self.treeview.enable_model_drag_source(gtk.gdk.BUTTON1_MASK,
            self._sourcetargets, gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_COPY)
        self.treeview.connect_after("drag-begin", self._cb_drag_begin)
        self.treeview.connect("drag_data_get", self._cb_tree_drag_data_get)
        
        vbox = gtk.VBox()
        vbox.set_border_width(20)
        vbox.set_spacing(20)
        # TC: The database tree view is being built (populated).
        label = gtk.Label(_('Populating'))
        vbox.pack_start(label, False, False, 0)
        self.tree_pb = gtk.ProgressBar()
        vbox.pack_start(self.tree_pb, False, False, 0)
        self.treealt.add(vbox)
        vbox.show_all()
        
        # Flat data view with search feature.
        # TC: User specified search filter entry box title text.
        filterframe = gtk.Frame(" %s " % _('Filters'))
        filterframe.set_shadow_type(gtk.SHADOW_OUT)
        filterframe.set_border_width(1)
        filterframe.set_label_align(0.5, 0.5)
        filterframe.show()
        filtervbox = gtk.VBox()
        filtervbox.set_border_width(3)
        filtervbox.set_spacing(1)
        filterframe.add(filtervbox)
        filtervbox.show()
        
        fuzzyhbox = gtk.HBox()
        filtervbox.pack_start(fuzzyhbox, False, False, 0)
        fuzzyhbox.show()
        # TC: A type of search on any data field matching paritial strings.
        fuzzylabel = gtk.Label(_('Fuzzy Search'))
        fuzzyhbox.pack_start(fuzzylabel, False, False, 0)
        fuzzylabel.show()
        self.fuzzyentry = gtk.Entry()
        self.fuzzyentry.connect("changed", self._cb_fuzzysearch_changed)
        fuzzyhbox.pack_start(self.fuzzyentry, True, True, 0)
        self.fuzzyentry.show()
        
        wherehbox = gtk.HBox()
        filtervbox.pack_start(wherehbox, False, False, 0)
        wherehbox.show()
        # TC: WHERE is an SQL keyword.
        wherelabel = gtk.Label(_('WHERE'))
        wherehbox.pack_start(wherelabel, False, False, 0)
        wherelabel.show()
        self.whereentry = gtk.Entry()
        self.whereentry.connect("activate", self._cb_update)
        wherehbox.pack_start(self.whereentry, True, True, 0)
        self.whereentry.show()
        image = gtk.image_new_from_stock(gtk.STOCK_EXECUTE,
                                                        gtk.ICON_SIZE_BUTTON)
        self.update = gtk.Button()
        self.update.connect("clicked", self._cb_update)
        self.update.set_image(image)
        image.show
        wherehbox.pack_start(self.update, False, False, 0)
        self.update.show()
        
        self.flatview, self.flatscroll, self.flatalt = self._makeview(
                                        self.notebook, _('Flat'), filterframe)
        self.flatview.set_rules_hint(True)
        self.flatview.set_rubber_banding(True)
        treeselection = self.flatview.get_selection()
        treeselection.set_mode(gtk.SELECTION_MULTIPLE)
        #                           found, id, ARTIST, ALBUM, TRACKNUM, TITLE,
        #                           DURATION, BITRATE, path, filename, disk
        self.flatstore = gtk.ListStore(
                            int, int, str, str, int, str, int, int, str, str, int)
        self.flatview.set_model(self.flatstore)
        self.flatcols = self._makecolumns(self.flatview, (
                ("(%d)" % 0, 0, self._cell_ralign, -1),
                (_('Artist'), 2, self._cell_show_unknown, 100),
                (_('Album'), 3, self._cell_show_unknown, 100),
                (_('Disk'), 10, self._cell_ralign, -1),
                (_('Track'), 4, self._cell_ralign, -1),
                (_('Title'), 5, self._cell_show_unknown, 100),
                (_('Duration'), 6, self._cell_secs_to_h_m_s, -1),
                (_('Bitrate'), 7, self._cell_k, -1),
                (_('Filename'), 8, None, 100),
                (_('Path'), 9, None, -1),
                ))

        self.flatview.enable_model_drag_source(gtk.gdk.BUTTON1_MASK,
            self._sourcetargets, gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_COPY)
        self.flatview.connect_after("drag-begin", self._cb_drag_begin)
        self.flatview.connect("drag_data_get", self._cb_flat_drag_data_get)

    def getcolwidths(self, cols):
        return ",".join([ str(x.get_width() or x.get_fixed_width())
                                                            for x in cols ])
    
    def setcolwidths(self, cols, data):
        c = cols.__iter__()
        for w in data.split(","):
            if w != "0":
                c.next().set_fixed_width(int(w))
            else:
                c.next()

    _sourcetargets = (
        ('text/plain', 0, 1),
        ('TEXT', 0, 2),
        ('STRING', 0, 3))

    def _cb_update(self, widget):
        print "ToDo cb_update"

    def _tree_select_func(self, info):
        return len(info) - 1

    def _cb_drag_begin(self, widget, context):
        context.set_icon_stock(gtk.STOCK_CDROM, -5, -5)

    def _cb_tree_drag_data_get(self, treeview, context, selection, target_id,
                                                                        etime):
        treeselection = treeview.get_selection()
        model, paths = treeselection.get_selected_rows()
        data = DNDAccumulator()
        if len(paths) == 1 and len(paths[0]) == 2:
            d2 = 0
            while 1:
                try:
                    iter = model.get_iter(paths[0] + (d2, ))
                except ValueError:
                    break
                data.append(model.get_value(iter, 6), model.get_value(iter, 5))
                d2 += 1
        else:
            for each in paths:
                if len(each) == 3:
                    iter = model.get_iter(each)
                    data.append(model.get_value(iter, 6),
                                                        model.get_value(iter,5))
        selection.set(selection.target, 8, str(data))

    def _cb_flat_drag_data_get(self, treeview, context, selection, target_id,
                                                                        etime):
        treeselection = treeview.get_selection()
        model, paths = treeselection.get_selected_rows()
        data = DNDAccumulator()
        for each in paths:
            iter = model.get_iter(each)
            data.append(model.get_value(iter, 9), model.get_value(iter, 8))
        selection.set(selection.target, 8, str(data))

    def _cb_fuzzysearch_changed(self, widget):
        if widget.get_text().strip():
            self.whereentry.set_sensitive(False)
        else:
            self.whereentry.set_sensitive(True)
        self.update.clicked()

    @staticmethod
    def _makeview(notebook, label_text, additional = None):
        vbox = gtk.VBox()
        vbox.set_spacing(2)
        scrollwindow = gtk.ScrolledWindow()
        alternate = gtk.VBox()
        vbox.pack_start(scrollwindow, True, True, 0)
        vbox.pack_start(alternate, True, True, 0)
        if additional is not None:
            vbox.pack_start(additional, False, False, 0)
        vbox.show()
        scrollwindow.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_ALWAYS)
        label = gtk.Label(label_text)
        notebook.append_page(vbox, label)
        label.show()
        scrollwindow.show()
        treeview = gtk.TreeView()
        scrollwindow.add(treeview)
        treeview.show()
        return treeview, scrollwindow, alternate

    @staticmethod
    def _makecolumns(view, name_ix_rf_mw):
        l = []
        for name, ix, rf, mw in name_ix_rf_mw:
            renderer = gtk.CellRendererText()
            column = gtk.TreeViewColumn(name, renderer)
            column.add_attribute(renderer, 'text', ix)
            if mw != -1:
                column.set_resizable(True)
                column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
                column.set_min_width(mw)
                column.set_fixed_width(mw + 50)
            view.append_column(column)
            l.append(column)
            if rf is not None:
                column.set_cell_data_func(renderer, rf, ix)
        return l
        
    def _cond_cell_secs_to_h_m_s(self, column, renderer, model, iter, cell):
        if model.get_value(iter, 0) >= 0:
            return self.cell_secs_to_h_m_s(column, renderer, model, iter, cell)
        else:
            renderer.set_property("text", "")
    
    def _cell_k(self, column, renderer, model, iter, cell):
        bitrate = model.get_value(iter, cell)
        if bitrate == 0:
            renderer.set_property("text", "")
        elif self.dbtype == "P3":
            renderer.set_property("text", "%dk" % bitrate)
        elif bitrate > 9999 and self.dbtype == "Ampache":
            renderer.set_property("text", "%dk" % (bitrate // 1000))
        renderer.set_property("xalign", 1.0)
    
    @staticmethod
    def _cell_show_unknown(column, renderer, model, iter, cell):
        if model.get_value(iter, cell) == "":
            # TC: Placeholder for unknown data.
            renderer.set_property("text", _('<unknown>'))
    
    @staticmethod
    def _cell_secs_to_h_m_s(column, renderer, model, iter, cell):
        v_in = model.get_value(iter, cell)
        m, s = divmod(v_in, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
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
        else:
            renderer.set_property("text", "")
