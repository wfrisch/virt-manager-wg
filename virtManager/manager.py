# Copyright (C) 2006-2008, 2013-2014 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf

from virtinst import log
from virtinst import xmlutil

from . import vmmenu
from .lib import uiutil
from .baseclass import vmmGObjectUI
from .connmanager import vmmConnectionManager
from .engine import vmmEngine
from .lib.graphwidgets import CellRendererSparkline

# Number of data points for performance graphs
GRAPH_LEN = 40

# fields in the tree model data set
(
    ROW_HANDLE,
    ROW_SORT_KEY,
    ROW_MARKUP,
    ROW_STATUS_ICON,
    ROW_HINT,
    ROW_IS_CONN,
    ROW_IS_CONN_CONNECTED,
    ROW_IS_VM,
    ROW_IS_VM_RUNNING,
    ROW_COLOR,
    ROW_INSPECTION_OS_ICON,
    ROW_IS_GROUP,
    ROW_GROUP_NAME,
) = range(13)

# GTK DnD target for moving a VM between groups
_DND_TARGET = "application/x-vmm-vm"

# Columns in the tree view
(COL_NAME, COL_GUEST_CPU, COL_HOST_CPU, COL_MEM, COL_DISK, COL_NETWORK) = range(6)


def _style_get_prop(widget, propname):
    value = GObject.Value()
    value.init(GObject.TYPE_INT)
    widget.style_get_property(propname, value)
    return value.get_int()


def _cmp(a, b):
    return (a > b) - (a < b)


def _get_inspection_icon_pixbuf(vm, w, h):
    # libguestfs gives us the PNG data as a string.
    png_data = vm.inspection.icon
    if png_data is None:
        return None

    try:
        pb = GdkPixbuf.PixbufLoader()
        pb.set_size(w, h)
        pb.write(png_data)
        pb.close()
        return pb.get_pixbuf()
    except Exception:  # pragma: no cover
        log.exception("Error loading inspection icon data")
        vm.inspection.icon = None
        return None


class vmmManager(vmmGObjectUI):
    @classmethod
    def get_instance(cls, parentobj):
        try:
            if not cls._instance:
                cls._instance = vmmManager()
            return cls._instance
        except Exception as e:  # pragma: no cover
            if not parentobj:
                raise
            parentobj.err.show_err(_("Error launching manager: %s") % str(e))

    def __init__(self):
        vmmGObjectUI.__init__(self, "manager.ui", "vmm-manager")
        self._cleanup_on_app_close()

        w, h = self.config.get_manager_window_size()
        self.topwin.set_default_size(w or 550, h or 550)
        self.prev_position = None
        self._window_size = None

        self.vmmenu = vmmenu.VMActionMenu(self, self.current_vm)
        self.shutdownmenu = vmmenu.VMShutdownMenu(self, self.current_vm)
        self.connmenu = Gtk.Menu()
        self.connmenu.get_accessible().set_name("conn-menu")
        self.connmenu_items = {}
        self.groupmenu = Gtk.Menu()
        self.groupmenu.get_accessible().set_name("group-menu")
        self.groupmenu_items = {}

        # Per-connection set of group names that have been created via the
        # "Add Group..." action but don't yet contain any VM. Session-only
        # per the design (no persistence for empty groups).
        # Key: connection URI -> set of group names
        self._pending_groups = {}

        self.builder.connect_signals(
            {
                "on_menu_view_guest_cpu_usage_activate": self.toggle_stats_visible_guest_cpu,
                "on_menu_view_host_cpu_usage_activate": self.toggle_stats_visible_host_cpu,
                "on_menu_view_memory_usage_activate": self.toggle_stats_visible_memory_usage,
                "on_menu_view_disk_io_activate": self.toggle_stats_visible_disk,
                "on_menu_view_network_traffic_activate": self.toggle_stats_visible_network,
                "on_vm_manager_delete_event": self.close,
                "on_vmm_manager_configure_event": self.window_resized,
                "on_menu_file_add_connection_activate": self.open_newconn,
                "on_menu_new_vm_activate": self.new_vm,
                "on_menu_file_quit_activate": self.exit_app,
                "on_menu_file_close_activate": self.close,
                "on_vmm_close_clicked": self.close,
                "on_vm_open_clicked": self.show_vm,
                "on_vm_run_clicked": self.start_vm,
                "on_vm_new_clicked": self.new_vm,
                "on_vm_shutdown_clicked": self.poweroff_vm,
                "on_vm_pause_clicked": self.pause_vm_button,
                "on_menu_edit_details_activate": self.show_vm,
                "on_menu_edit_delete_activate": self.do_delete,
                "on_menu_host_details_activate": self.show_host,
                "on_vm_list_row_activated": self.row_activated,
                "on_vm_list_button_press_event": self.popup_vm_menu_button,
                "on_vm_list_key_press_event": self.popup_vm_menu_key,
                "on_menu_edit_preferences_activate": self.show_preferences,
                "on_menu_help_about_activate": self.show_about,
            }
        )

        # There seem to be ref counting issues with calling
        # list.get_column, so avoid it
        self.diskcol = None
        self.netcol = None
        self.memcol = None
        self.guestcpucol = None
        self.hostcpucol = None
        self.spacer_txt = None
        self.init_vmlist()

        self.init_stats()
        self.init_toolbar()
        self.init_context_menus()

        self.update_current_selection()
        self.widget("vm-list").get_selection().connect("changed", self.update_current_selection)

        self.max_disk_rate = 10.0
        self.max_net_rate = 10.0

        # Initialize stat polling columns based on global polling
        # preferences (we want signal handlers for this)
        self._config_polling_change_cb(COL_GUEST_CPU)
        self._config_polling_change_cb(COL_DISK)
        self._config_polling_change_cb(COL_NETWORK)
        self._config_polling_change_cb(COL_MEM)

        connmanager = vmmConnectionManager.get_instance()
        connmanager.connect("conn-added", self._conn_added)
        connmanager.connect("conn-removed", self._conn_removed)
        for conn in connmanager.conns.values():
            self._conn_added(connmanager, conn)

    ##################
    # Common methods #
    ##################

    def show(self):
        vis = self.is_visible()
        self.topwin.present()
        if vis:
            return

        log.debug("Showing manager")
        if self.prev_position:
            self.topwin.move(*self.prev_position)
            self.prev_position = None

        vmmEngine.get_instance().increment_window_counter()

    def close(self, src_ignore=None, src2_ignore=None):
        if not self.is_visible():
            return

        log.debug("Closing manager")
        self.prev_position = self.topwin.get_position()
        self.topwin.hide()
        vmmEngine.get_instance().decrement_window_counter()

        return 1

    def _cleanup(self):
        self.diskcol = None
        self.guestcpucol = None
        self.memcol = None
        self.hostcpucol = None
        self.netcol = None

        self.shutdownmenu.destroy()
        self.shutdownmenu = None
        self.vmmenu.destroy()
        self.vmmenu = None
        self.connmenu.destroy()
        self.connmenu = None
        self.connmenu_items = None
        self.groupmenu.destroy()
        self.groupmenu = None
        self.groupmenu_items = None
        self._pending_groups = None

        if self._window_size:
            self.config.set_manager_window_size(*self._window_size)

    def set_startup_error(self, msg):
        self.widget("vm-notebook").set_current_page(1)
        self.widget("startup-error-label").set_text(msg)

    ################
    # Init methods #
    ################

    def init_stats(self):
        self.add_gsettings_handle(
            self.config.on_vmlist_guest_cpu_usage_visible_changed(
                self.toggle_guest_cpu_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_host_cpu_usage_visible_changed(
                self.toggle_host_cpu_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_memory_usage_visible_changed(
                self.toggle_memory_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_disk_io_visible_changed(self.toggle_disk_io_visible_widget)
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_network_traffic_visible_changed(
                self.toggle_network_traffic_visible_widget
            )
        )

        # Register callbacks with the global stats enable/disable values
        # that disable the associated vmlist widgets if reporting is disabled
        self.add_gsettings_handle(
            self.config.on_stats_enable_cpu_poll_changed(
                self._config_polling_change_cb, COL_GUEST_CPU
            )
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_disk_poll_changed(self._config_polling_change_cb, COL_DISK)
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_net_poll_changed(
                self._config_polling_change_cb, COL_NETWORK
            )
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_memory_poll_changed(self._config_polling_change_cb, COL_MEM)
        )

        self.toggle_guest_cpu_usage_visible_widget()
        self.toggle_host_cpu_usage_visible_widget()
        self.toggle_memory_usage_visible_widget()
        self.toggle_disk_io_visible_widget()
        self.toggle_network_traffic_visible_widget()

    def init_toolbar(self):
        self.widget("vm-new").set_icon_name("vm_new")
        self.widget("vm-open").set_icon_name("icon_console")

        self.widget("vm-shutdown").set_icon_name("system-shutdown")
        self.widget("vm-shutdown").set_menu(self.shutdownmenu)

        tool = self.widget("vm-toolbar")
        tool.set_property("icon-size", Gtk.IconSize.LARGE_TOOLBAR)
        for c in tool.get_children():
            c.set_homogeneous(False)

    def init_context_menus(self):
        def add_to_conn_menu(idx, text, cb):
            item = Gtk.MenuItem.new_with_mnemonic(text)
            if cb:
                item.connect("activate", cb)
            item.get_accessible().set_name("conn-%s" % idx)
            self.connmenu.add(item)
            self.connmenu_items[idx] = item

        # Build connection context menu
        add_to_conn_menu("create", _("_New"), self.new_vm)
        add_to_conn_menu("connect", _("_Connect"), self.open_conn)
        add_to_conn_menu("disconnect", _("Dis_connect"), self.close_conn)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_conn_menu("add-group", _("Add _Group..."), self.add_group)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_conn_menu("delete", _("De_lete"), self.do_delete)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_conn_menu("details", _("_Details"), self.show_host)
        self.connmenu.show_all()

        # Build group context menu
        def add_to_group_menu(idx, text, cb):
            item = Gtk.MenuItem.new_with_mnemonic(text)
            if cb:
                item.connect("activate", cb)
            item.get_accessible().set_name("group-%s" % idx)
            self.groupmenu.add(item)
            self.groupmenu_items[idx] = item

        add_to_group_menu("delete", _("De_lete Group"), self.delete_group)
        self.groupmenu.show_all()

    def init_vmlist(self):
        vmlist = self.widget("vm-list")
        self.widget("vm-notebook").set_show_tabs(False)

        rowtypes = []
        rowtypes.insert(ROW_HANDLE, object)  # backing object
        rowtypes.insert(ROW_SORT_KEY, str)  # object name
        rowtypes.insert(ROW_MARKUP, str)  # row markup text
        rowtypes.insert(ROW_STATUS_ICON, str)  # status icon name
        rowtypes.insert(ROW_HINT, str)  # row tooltip
        rowtypes.insert(ROW_IS_CONN, bool)  # if object is a connection
        rowtypes.insert(ROW_IS_CONN_CONNECTED, bool)  # if conn is connected
        rowtypes.insert(ROW_IS_VM, bool)  # if row is VM
        rowtypes.insert(ROW_IS_VM_RUNNING, bool)  # if VM is running
        rowtypes.insert(ROW_COLOR, str)  # row markup color string
        rowtypes.insert(ROW_INSPECTION_OS_ICON, GdkPixbuf.Pixbuf)  # OS icon
        rowtypes.insert(ROW_IS_GROUP, bool)  # if row is a group folder
        rowtypes.insert(ROW_GROUP_NAME, str)  # group name (empty for non-group)

        model = Gtk.TreeStore(*rowtypes)
        vmlist.set_model(model)
        vmlist.set_tooltip_column(ROW_HINT)
        vmlist.set_headers_visible(True)
        # Use GTK's default per-level indent so VMs visibly nest under their
        # group (and groups under their connection). The previous code used a
        # negative offset to flatten the 2-level tree; that hides the new
        # group→VM hierarchy.
        vmlist.set_level_indentation(0)

        nameCol = Gtk.TreeViewColumn(_("Name"))
        nameCol.set_expand(True)
        nameCol.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        nameCol.set_spacing(6)
        nameCol.set_sort_column_id(COL_NAME)

        vmlist.append_column(nameCol)

        status_icon = Gtk.CellRendererPixbuf()
        status_icon.set_property("stock-size", Gtk.IconSize.DND)
        nameCol.pack_start(status_icon, False)
        nameCol.add_attribute(status_icon, "icon-name", ROW_STATUS_ICON)
        nameCol.add_attribute(status_icon, "visible", ROW_IS_VM)

        inspection_os_icon = Gtk.CellRendererPixbuf()
        nameCol.pack_start(inspection_os_icon, False)
        nameCol.add_attribute(inspection_os_icon, "pixbuf", ROW_INSPECTION_OS_ICON)
        nameCol.add_attribute(inspection_os_icon, "visible", ROW_IS_VM)

        name_txt = Gtk.CellRendererText()
        nameCol.pack_start(name_txt, True)
        nameCol.add_attribute(name_txt, "markup", ROW_MARKUP)
        nameCol.add_attribute(name_txt, "foreground", ROW_COLOR)

        self.spacer_txt = Gtk.CellRendererText()
        self.spacer_txt.set_property("ypad", 4)
        self.spacer_txt.set_property("visible", False)
        nameCol.pack_end(self.spacer_txt, False)

        def make_stats_column(title, colnum):
            col = Gtk.TreeViewColumn(title)
            col.set_min_width(140)

            txt = Gtk.CellRendererText()
            txt.set_property("ypad", 4)
            col.pack_start(txt, True)
            col.add_attribute(txt, "visible", ROW_IS_CONN)

            img = CellRendererSparkline()
            img.set_property("xpad", 6)
            img.set_property("ypad", 12)
            img.set_property("reversed", True)
            col.pack_start(img, True)
            col.add_attribute(img, "visible", ROW_IS_VM)

            col.set_sort_column_id(colnum)
            vmlist.append_column(col)
            return col

        self.guestcpucol = make_stats_column(_("CPU usage"), COL_GUEST_CPU)
        self.hostcpucol = make_stats_column(_("Host CPU usage"), COL_HOST_CPU)
        self.memcol = make_stats_column(_("Memory usage"), COL_MEM)
        self.diskcol = make_stats_column(_("Disk I/O"), COL_DISK)
        self.netcol = make_stats_column(_("Network I/O"), COL_NETWORK)

        model.set_sort_func(COL_NAME, self.vmlist_name_sorter)
        model.set_sort_func(COL_GUEST_CPU, self.vmlist_guest_cpu_usage_sorter)
        model.set_sort_func(COL_HOST_CPU, self.vmlist_host_cpu_usage_sorter)
        model.set_sort_func(COL_MEM, self.vmlist_memory_usage_sorter)
        model.set_sort_func(COL_DISK, self.vmlist_disk_io_sorter)
        model.set_sort_func(COL_NETWORK, self.vmlist_network_usage_sorter)
        model.set_sort_column_id(COL_NAME, Gtk.SortType.ASCENDING)

        # Drag-and-drop: VMs can be dragged onto group or connection rows
        # to change their group assignment.
        targets = [Gtk.TargetEntry.new(_DND_TARGET, Gtk.TargetFlags.SAME_APP, 0)]
        vmlist.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, targets, Gdk.DragAction.MOVE)
        vmlist.enable_model_drag_dest(targets, Gdk.DragAction.MOVE)
        vmlist.connect("drag-data-get", self._vmlist_drag_data_get)
        vmlist.connect("drag-data-received", self._vmlist_drag_data_received)

    ##################
    # Helper methods #
    ##################

    @property
    def model(self):
        return self.widget("vm-list").get_model()

    def current_row(self):
        return uiutil.get_list_selected_row(self.widget("vm-list"))

    def current_vm(self):
        row = self.current_row()
        if not row or row[ROW_IS_CONN] or row[ROW_IS_GROUP]:
            return None

        return row[ROW_HANDLE]

    def current_conn(self):
        row = self.current_row()
        if not row:
            return None
        handle = row[ROW_HANDLE]
        if row[ROW_IS_CONN]:
            return handle
        if row[ROW_IS_GROUP]:
            # Group rows store the parent connection in ROW_HANDLE
            return handle
        return handle.conn

    def current_group(self):
        """Return the group row at current selection, or None."""
        row = self.current_row()
        if not row or not row[ROW_IS_GROUP]:
            return None
        return row

    def get_row(self, conn_or_vm):
        def _walk(model, rowiter, obj):
            while rowiter:
                row = model[rowiter]
                if row[ROW_HANDLE] == obj:
                    return row
                if model.iter_has_child(rowiter):
                    ret = _walk(model, model.iter_nth_child(rowiter, 0), obj)
                    if ret:
                        return ret
                rowiter = model.iter_next(rowiter)

        if not len(self.model):
            return None
        return _walk(self.model, self.model.get_iter_first(), conn_or_vm)

    ####################
    # Action listeners #
    ####################

    def window_resized(self, ignore, ignore2):
        if not self.is_visible():
            return
        self._window_size = self.topwin.get_size()

    def exit_app(self, src_ignore=None, src2_ignore=None):
        vmmEngine.get_instance().exit_app()

    def open_newconn(self, _src):
        from .createconn import vmmCreateConn

        vmmCreateConn.get_instance(self).show(self.topwin)

    def new_vm(self, _src):
        from .createvm import vmmCreateVM

        conn = self.current_conn()
        vmmCreateVM.show_instance(self, conn and conn.get_uri() or None)

    def show_about(self, _src):
        from .about import vmmAbout

        vmmAbout.show_instance(self)

    def show_preferences(self, src_ignore):
        from .preferences import vmmPreferences

        vmmPreferences.show_instance(self)

    def show_host(self, _src):
        from .host import vmmHost

        conn = self.current_conn()
        vmmHost.show_instance(self, conn)

    def show_vm(self, _src):
        vmmenu.VMActionUI.show(self, self.current_vm())

    def row_activated(self, _src, *args):
        ignore = args
        conn = self.current_conn()
        vm = self.current_vm()
        if conn is None:
            return  # pragma: no cover

        if vm:
            self.show_vm(_src)
        elif conn.is_disconnected():
            self.open_conn()
        else:
            self.show_host(_src)

    def do_delete(self, ignore=None):
        if self.current_group() is not None:
            self.delete_group()
            return
        conn = self.current_conn()
        vm = self.current_vm()
        if vm is None:
            self._do_delete_conn(conn)
        else:
            vmmenu.VMActionUI.delete(self, vm)

    def _do_delete_conn(self, conn):
        result = self.err.yes_no(
            _("This will remove the connection:\n\n%s\n\nAre you sure?") % conn.get_uri()
        )
        if not result:
            return

        vmmConnectionManager.get_instance().remove_conn(conn.get_uri())

    #####################
    # Group action menus #
    #####################

    def _prompt_group_name(self, conn):
        """Show a small dialog asking the user for a new group name."""
        dialog = Gtk.Dialog(
            title=_("Add Group"),
            transient_for=self.topwin,
            modal=True,
        )
        dialog.add_buttons(
            _("_Cancel"),
            Gtk.ResponseType.CANCEL,
            _("_OK"),
            Gtk.ResponseType.OK,
        )
        dialog.set_default_response(Gtk.ResponseType.OK)

        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        label = Gtk.Label(label=_("Group name:"))
        label.set_xalign(0)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        entry.get_accessible().set_name("group-name-entry")
        box.add(label)
        box.add(entry)
        box.show_all()

        try:
            while True:
                response = dialog.run()
                if response != Gtk.ResponseType.OK:
                    return None
                name = entry.get_text().strip()
                if not name:
                    self.err.show_err(_("Group name cannot be empty."))
                    continue
                # Check uniqueness vs current groups (pending or VM-derived)
                existing = self._collect_group_names(conn)
                if name in existing:
                    self.err.show_err(_("A group named '%s' already exists.") % name)
                    continue
                return name
        finally:
            dialog.destroy()

    def _collect_group_names(self, conn):
        """Return the set of group names currently visible under the conn."""
        names = set(self._pending_groups.get(conn.get_uri(), set()))
        for vm in conn.list_vms():
            g = vm.get_group()
            if g:
                names.add(g)
        return names

    def add_group(self, _src=None):
        conn = self.current_conn()
        if conn is None or conn.is_disconnected():
            return
        name = self._prompt_group_name(conn)
        if name is None:
            return

        self._pending_groups.setdefault(conn.get_uri(), set()).add(name)
        group_iter = self._get_or_create_group_iter(conn, name)
        if group_iter is None:
            return

        # Expand the conn so the new group is visible, and select it
        conn_row = self.get_row(conn)
        self.widget("vm-list").expand_row(conn_row.path, False)
        sel = self.widget("vm-list").get_selection()
        sel.select_path(self.model.get_path(group_iter))

    def delete_group(self, _src=None):
        group_row = self.current_group()
        if group_row is None:
            return
        if self.model.iter_n_children(group_row.iter) > 0:
            self.err.show_err(_("Cannot delete a non-empty group. Move its VMs out first."))
            return

        conn = group_row[ROW_HANDLE]
        name = group_row[ROW_GROUP_NAME]
        self._pending_groups.get(conn.get_uri(), set()).discard(name)
        self.model.remove(group_row.iter)

    ###################
    # Drag-and-drop   #
    ###################

    def _vmlist_drag_data_get(self, _widget, _ctx, selection, _info, _time):
        row = self.current_row()
        if row is None or not row[ROW_IS_VM]:
            return
        vm = row[ROW_HANDLE]
        payload = "%s\x00%s" % (vm.conn.get_uri(), vm.get_uuid())
        selection.set(selection.get_target(), 8, payload.encode("utf-8"))

    def _vmlist_drag_data_received(self, widget, _ctx, x, y, selection, _info, _time):
        data = selection.get_data()
        if not data:
            return
        try:
            uri, uuid = data.decode("utf-8").split("\x00", 1)
        except (UnicodeDecodeError, ValueError):
            return

        drop_info = widget.get_dest_row_at_pos(x, y)
        if drop_info is None:
            # Dropped on empty area -> treat as "ungrouped" if connection
            # is selected, otherwise ignore
            target_iter = None
        else:
            path, _pos = drop_info
            target_iter = self.model.get_iter(path)

        if target_iter is None:
            return
        target_row = self.model[target_iter]

        # Determine the target group + connection
        if target_row[ROW_IS_GROUP]:
            target_conn = target_row[ROW_HANDLE]
            target_group = target_row[ROW_GROUP_NAME]
        elif target_row[ROW_IS_CONN]:
            target_conn = target_row[ROW_HANDLE]
            target_group = ""
        elif target_row[ROW_IS_VM]:
            target_vm = target_row[ROW_HANDLE]
            target_conn = target_vm.conn
            target_group = target_vm.get_group()
        else:
            return

        if target_conn.get_uri() != uri:
            self.err.show_err(_("Cannot move a VM between connections via drag-and-drop."))
            return

        # Locate the source VM
        src_vm = None
        for vm in target_conn.list_vms():
            if vm.get_uuid() == uuid:
                src_vm = vm
                break
        if src_vm is None:
            return

        try:
            src_vm.set_group(target_group)
        except Exception as e:
            self.err.show_err(
                _("Error moving VM to group '%(group)s': %(error)s")
                % {"group": target_group or _("(ungrouped)"), "error": str(e)}
            )
            return

        # set_group only updates XML — trigger a UI refresh so the row reparents
        self._reparent_vm_if_needed(src_vm)

    def set_pause_state(self, state):
        src = self.widget("vm-pause")
        try:
            src.handler_block_by_func(self.pause_vm_button)
            src.set_active(state)
        finally:
            src.handler_unblock_by_func(self.pause_vm_button)

    def pause_vm_button(self, src):
        do_pause = src.get_active()

        # Set button state back to original value: just let the status
        # update function fix things for us
        self.set_pause_state(not do_pause)

        if do_pause:
            vmmenu.VMActionUI.suspend(self, self.current_vm())
        else:
            vmmenu.VMActionUI.resume(self, self.current_vm())

    def start_vm(self, ignore):
        vmmenu.VMActionUI.run(self, self.current_vm())

    def poweroff_vm(self, _src):
        vmmenu.VMActionUI.shutdown(self, self.current_vm())

    def close_conn(self, ignore):
        conn = self.current_conn()
        if not conn.is_disconnected():
            conn.close()

    def open_conn(self, ignore=None):
        conn = self.current_conn()
        if conn.is_disconnected():
            conn.connect_once("open-completed", self._conn_open_completed_cb)
            conn.open()
            return True

    def _conn_open_completed_cb(self, _conn, ConnectError):
        if ConnectError:
            msg, details, title = ConnectError
            self.err.show_err(msg, details, title)

    ####################################
    # VM add/remove management methods #
    ####################################

    def vm_added(self, conn, vm):
        vm_row = self._build_row(None, vm)
        conn_row = self.get_row(conn)
        parent_iter = self._get_or_create_group_iter(conn, vm.get_group())
        self.model.append(parent_iter, vm_row)

        vm.connect("state-changed", self.vm_changed)
        vm.connect("resources-sampled", self.vm_row_updated)
        vm.connect("inspection-changed", self.vm_inspection_changed)

        # Expand the connection (and the group, if any) when adding a vm
        self.widget("vm-list").expand_row(conn_row.path, False)
        if vm.get_group():
            group_path = self.model.get_path(parent_iter)
            self.widget("vm-list").expand_row(group_path, False)

    def vm_removed(self, conn, vm):
        row = self.get_row(vm)
        if row is None:
            return  # pragma: no cover
        old_group = ""
        parent_iter = self.model.iter_parent(row.iter)
        if parent_iter is not None and self.model[parent_iter][ROW_IS_GROUP]:
            old_group = self.model[parent_iter][ROW_GROUP_NAME]
        self.model.remove(row.iter)
        self._maybe_remove_group_row(conn, old_group)

    def _build_conn_hint(self, conn):
        hint = conn.get_uri()
        if conn.is_disconnected():
            hint = _("%(uri)s (Double click to connect)") % {"uri": conn.get_uri()}
        return hint

    def _build_conn_markup(self, conn, name):
        name = xmlutil.xml_escape(name)
        text = name
        if conn.is_disconnected():
            text = _("%(connection)s - Not Connected") % {"connection": name}
        elif conn.is_connecting():
            text = _("%(connection)s - Connecting...") % {"connection": name}

        markup = "<span size='smaller'>%s</span>" % text
        return markup

    def _build_conn_color(self, conn):
        color = None
        if conn.is_disconnected():
            color = self.config.color_insensitive
        return color

    def _build_vm_markup(self, name, status):
        domtext = "<span size='smaller' weight='bold'>%s</span>" % xmlutil.xml_escape(name)
        statetext = "<span size='smaller'>%s</span>" % status
        return domtext + "\n" + statetext

    def _build_row(self, conn, vm):
        if conn:
            name = conn.get_pretty_desc()
            markup = self._build_conn_markup(conn, name)
            status = "<span size='smaller'>%s</span>" % conn.get_state_text()
            status_icon = None
            hint = self._build_conn_hint(conn)
            color = self._build_conn_color(conn)
            os_icon = None
        else:
            name = vm.get_name_or_title()
            status = vm.run_status()
            markup = self._build_vm_markup(name, status)
            status_icon = vm.run_status_icon_name()
            hint = vm.get_description()
            color = None
            os_icon = _get_inspection_icon_pixbuf(vm, 16, 16)

        row = []
        row.insert(ROW_HANDLE, conn or vm)
        row.insert(ROW_SORT_KEY, name)
        row.insert(ROW_MARKUP, markup)
        row.insert(ROW_STATUS_ICON, status_icon)
        row.insert(ROW_HINT, xmlutil.xml_escape(hint))
        row.insert(ROW_IS_CONN, bool(conn))
        row.insert(ROW_IS_CONN_CONNECTED, bool(conn) and not conn.is_disconnected())
        row.insert(ROW_IS_VM, bool(vm))
        row.insert(ROW_IS_VM_RUNNING, bool(vm) and vm.is_active())
        row.insert(ROW_COLOR, color)
        row.insert(ROW_INSPECTION_OS_ICON, os_icon)
        row.insert(ROW_IS_GROUP, False)
        row.insert(ROW_GROUP_NAME, "")

        return row

    def _build_group_row(self, conn, group_name):
        markup = "<span size='smaller' weight='bold'>%s</span>" % xmlutil.xml_escape(group_name)
        row = []
        row.insert(ROW_HANDLE, conn)
        row.insert(ROW_SORT_KEY, group_name)
        row.insert(ROW_MARKUP, markup)
        row.insert(ROW_STATUS_ICON, "folder")
        row.insert(ROW_HINT, xmlutil.xml_escape(group_name))
        row.insert(ROW_IS_CONN, False)
        row.insert(ROW_IS_CONN_CONNECTED, False)
        row.insert(ROW_IS_VM, False)
        row.insert(ROW_IS_VM_RUNNING, False)
        row.insert(ROW_COLOR, None)
        row.insert(ROW_INSPECTION_OS_ICON, None)
        row.insert(ROW_IS_GROUP, True)
        row.insert(ROW_GROUP_NAME, group_name)
        return row

    def _find_group_iter(self, conn_iter, group_name):
        """Return the TreeIter of the named group under conn_iter, or None."""
        rowiter = self.model.iter_children(conn_iter)
        while rowiter is not None:
            row = self.model[rowiter]
            if row[ROW_IS_GROUP] and row[ROW_GROUP_NAME] == group_name:
                return rowiter
            rowiter = self.model.iter_next(rowiter)
        return None

    def _get_or_create_group_iter(self, conn, group_name):
        """
        Return the TreeIter under which a VM with the given group name should
        live. Empty group name means "ungrouped" -> the connection iter itself.
        """
        conn_row = self.get_row(conn)
        if conn_row is None:
            return None
        if not group_name:
            return conn_row.iter
        existing = self._find_group_iter(conn_row.iter, group_name)
        if existing is not None:
            return existing
        return self.model.append(conn_row.iter, self._build_group_row(conn, group_name))

    def _maybe_remove_group_row(self, conn, group_name):
        """
        Remove the group row if it has no VM children and the user hasn't
        explicitly created it via "Add Group...".
        """
        if not group_name:
            return
        if group_name in self._pending_groups.get(conn.get_uri(), set()):
            return
        conn_row = self.get_row(conn)
        if conn_row is None:
            return
        group_iter = self._find_group_iter(conn_row.iter, group_name)
        if group_iter is None:
            return
        if self.model.iter_n_children(group_iter) == 0:
            self.model.remove(group_iter)

    def _conn_added(self, _src, conn):
        # Make sure error page isn't showing
        self.widget("vm-notebook").set_current_page(0)
        if self.get_row(conn):
            return  # pragma: no cover

        conn_row = self._build_row(conn, None)
        self.model.append(None, conn_row)

        conn.connect("vm-added", self.vm_added)
        conn.connect("vm-removed", self.vm_removed)
        conn.connect("resources-sampled", self.conn_row_updated)
        conn.connect("state-changed", self.conn_state_changed)

        for vm in conn.list_vms():
            self.vm_added(conn, vm)

    def _remove_child_rows(self, row):
        child = self.model.iter_children(row.iter)
        while child is not None:  # pragma: no cover
            # vm-removed signals should handle this, this is a fallback
            # in case something goes wrong
            self.model.remove(child)
            child = self.model.iter_children(row.iter)

    def _conn_removed(self, _src, uri):
        conn_row = None
        for row in self.model:
            if row[ROW_IS_CONN] and row[ROW_HANDLE].get_uri() == uri:
                conn_row = row
                break
        if conn_row is None:  # pragma: no cover
            return

        self._remove_child_rows(conn_row)
        self.model.remove(conn_row.iter)
        self._pending_groups.pop(uri, None)

    #############################
    # State/UI updating methods #
    #############################

    def vm_row_updated(self, vm):
        row = self.get_row(vm)
        if row is None:  # pragma: no cover
            return
        self.model.row_changed(row.path, row.iter)

    def vm_changed(self, vm):
        row = self.get_row(vm)
        if row is None:
            return  # pragma: no cover

        try:
            if vm == self.current_vm():
                self.update_current_selection()

            name = vm.get_name_or_title()
            status = vm.run_status()

            row[ROW_SORT_KEY] = name
            row[ROW_STATUS_ICON] = vm.run_status_icon_name()
            row[ROW_IS_VM_RUNNING] = vm.is_active()
            row[ROW_MARKUP] = self._build_vm_markup(name, status)

            desc = vm.get_description()
            row[ROW_HINT] = xmlutil.xml_escape(desc)

            # Reparent if the VM's group assignment in XML has changed
            self._reparent_vm_if_needed(vm)
        except Exception as e:  # pragma: no cover
            if vm.conn.support.is_libvirt_error_no_domain(e):
                return
            raise

        self.vm_row_updated(vm)

    def _reparent_vm_if_needed(self, vm):
        row = self.get_row(vm)
        if row is None:
            return
        parent_iter = self.model.iter_parent(row.iter)
        cur_group = ""
        if parent_iter is not None and self.model[parent_iter][ROW_IS_GROUP]:
            cur_group = self.model[parent_iter][ROW_GROUP_NAME]
        new_group = vm.get_group()
        if cur_group == new_group:
            return

        # Rebuild the row at the new location, then drop the old one
        new_parent_iter = self._get_or_create_group_iter(vm.conn, new_group)
        new_row = self._build_row(None, vm)
        new_iter = self.model.append(new_parent_iter, new_row)
        self.model.remove(row.iter)

        # Select the new row and ensure it's visible
        sel = self.widget("vm-list").get_selection()
        new_path = self.model.get_path(new_iter)
        self.widget("vm-list").expand_to_path(new_path)
        sel.select_path(new_path)

        # The old group might now be empty
        self._maybe_remove_group_row(vm.conn, cur_group)

    def vm_inspection_changed(self, vm):
        row = self.get_row(vm)
        if row is None:
            return  # pragma: no cover

        new_icon = _get_inspection_icon_pixbuf(vm, 16, 16)
        row[ROW_INSPECTION_OS_ICON] = new_icon

        self.vm_row_updated(vm)

    def set_initial_selection(self, uri):
        """
        Select the passed URI in the UI. Called from engine.py via
        cli --connect $URI
        """
        sel = self.widget("vm-list").get_selection()
        for row in self.model:
            if not row[ROW_IS_CONN]:
                continue  # pragma: no cover
            conn = row[ROW_HANDLE]

            if conn.get_uri() == uri:
                sel.select_iter(row.iter)
                return

    def conn_state_changed(self, conn):
        row = self.get_row(conn)
        row[ROW_SORT_KEY] = conn.get_pretty_desc()
        row[ROW_MARKUP] = self._build_conn_markup(conn, row[ROW_SORT_KEY])
        row[ROW_IS_CONN_CONNECTED] = not conn.is_disconnected()
        row[ROW_COLOR] = self._build_conn_color(conn)
        row[ROW_HINT] = self._build_conn_hint(conn)

        if not conn.is_active():
            self._remove_child_rows(row)
            self._pending_groups.pop(conn.get_uri(), None)

        self.conn_row_updated(conn)
        self.update_current_selection()

    def conn_row_updated(self, conn):
        row = self.get_row(conn)

        self.max_disk_rate = max(self.max_disk_rate, conn.disk_io_max_rate())
        self.max_net_rate = max(self.max_net_rate, conn.network_traffic_max_rate())

        self.model.row_changed(row.path, row.iter)

    def change_run_text(self, can_restore):
        if can_restore:
            text = _("_Restore")
        else:
            text = _("_Run")
        strip_text = text.replace("_", "")

        self.vmmenu.change_run_text(text)
        self.widget("vm-run").set_label(strip_text)

    def update_current_selection(self, ignore=None):
        vm = self.current_vm()
        conn = self.current_conn()

        show_open = bool(vm)
        show_details = bool(vm)
        host_details = bool(vm or conn)
        can_delete = bool(vm or conn)

        show_run = bool(vm and vm.is_runable())
        is_paused = bool(vm and vm.is_paused())
        if is_paused:
            show_pause = bool(vm and vm.is_unpauseable())
        else:
            show_pause = bool(vm and vm.is_pauseable())
        show_shutdown = bool(vm and vm.is_stoppable())

        if vm and vm.managedsave_supported:
            self.change_run_text(vm.has_managed_save())

        self.widget("vm-open").set_sensitive(show_open)
        self.widget("vm-run").set_sensitive(show_run)
        self.widget("vm-shutdown").set_sensitive(show_shutdown)
        self.widget("vm-shutdown").get_menu().update_widget_states(vm)

        self.set_pause_state(is_paused)
        self.widget("vm-pause").set_sensitive(show_pause)

        if is_paused:
            pauseTooltip = _("Resume the virtual machine")
        else:
            pauseTooltip = _("Pause the virtual machine")
        self.widget("vm-pause").set_tooltip_text(pauseTooltip)

        self.widget("menu_edit_delete").set_sensitive(can_delete)
        self.widget("menu_edit_details").set_sensitive(show_details)
        self.widget("menu_host_details").set_sensitive(host_details)

    def popup_vm_menu_key(self, widget_ignore, event):
        if Gdk.keyval_name(event.keyval) != "Menu":
            return False  # pragma: no cover

        model, treeiter = self.widget("vm-list").get_selection().get_selected()
        self.popup_vm_menu(model, treeiter, event)
        return True

    def popup_vm_menu_button(self, vmlist, event):
        if event.button != 3:
            return False

        tup = vmlist.get_path_at_pos(int(event.x), int(event.y))
        if tup is None:
            return False  # pragma: no cover
        path = tup[0]

        self.popup_vm_menu(self.model, self.model.get_iter(path), event)
        return False

    def popup_vm_menu(self, model, _iter, event):
        row = model[_iter]
        if row[ROW_IS_GROUP]:
            # Pop up group menu. Only allow delete when the group has no VMs.
            has_children = model.iter_n_children(_iter) > 0
            self.groupmenu_items["delete"].set_sensitive(not has_children)
            self.groupmenu.popup_at_pointer(event)
        elif row[ROW_IS_VM]:
            # Popup the vm menu
            vm = row[ROW_HANDLE]
            self.vmmenu.update_widget_states(vm)
            self.vmmenu.popup_at_pointer(event)
        else:
            # Pop up connection menu
            conn = row[ROW_HANDLE]
            disconn = conn.is_disconnected()
            conning = conn.is_connecting()

            self.connmenu_items["create"].set_sensitive(not disconn)
            self.connmenu_items["disconnect"].set_sensitive(not (disconn or conning))
            self.connmenu_items["connect"].set_sensitive(disconn)
            self.connmenu_items["delete"].set_sensitive(disconn)
            self.connmenu_items["add-group"].set_sensitive(not disconn)

            self.connmenu.popup_at_pointer(event)

    #################
    # Stats methods #
    #################

    def vmlist_name_sorter(self, model, iter1, iter2, ignore):
        # Group rows always sort before non-group rows at the same level
        is_group1 = bool(model[iter1][ROW_IS_GROUP])
        is_group2 = bool(model[iter2][ROW_IS_GROUP])
        if is_group1 != is_group2:
            return -1 if is_group1 else 1
        key1 = str(model[iter1][ROW_SORT_KEY]).lower()
        key2 = str(model[iter2][ROW_SORT_KEY]).lower()
        return _cmp(key1, key2)

    def vmlist_guest_cpu_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.guest_cpu_time_percentage(), obj2.guest_cpu_time_percentage())

    def vmlist_host_cpu_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.host_cpu_time_percentage(), obj2.host_cpu_time_percentage())

    def vmlist_memory_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.stats_memory(), obj2.stats_memory())

    def vmlist_disk_io_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.disk_io_rate(), obj2.disk_io_rate())

    def vmlist_network_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.network_traffic_rate(), obj2.network_traffic_rate())

    def _config_polling_change_cb(self, column):
        # pylint: disable=redefined-variable-type
        if column == COL_GUEST_CPU:
            widgn = ["menu_view_stats_guest_cpu", "menu_view_stats_host_cpu"]
            do_enable = self.config.get_stats_enable_cpu_poll()
        if column == COL_DISK:
            widgn = "menu_view_stats_disk"
            do_enable = self.config.get_stats_enable_disk_poll()
        elif column == COL_NETWORK:
            widgn = "menu_view_stats_network"
            do_enable = self.config.get_stats_enable_net_poll()
        elif column == COL_MEM:
            widgn = "menu_view_stats_memory"
            do_enable = self.config.get_stats_enable_memory_poll()

        for w in xmlutil.listify(widgn):
            widget = self.widget(w)
            tool_text = ""

            if do_enable:
                widget.set_sensitive(True)
            else:
                widget.set_active(False)
                widget.set_sensitive(False)
                tool_text = _("Disabled in preferences dialog.")
            widget.set_tooltip_text(tool_text)

    def _toggle_graph_helper(self, do_show, col, datafunc, menu):
        img = -1
        for child in col.get_cells():
            if isinstance(child, CellRendererSparkline):
                img = child
        datafunc = do_show and datafunc or None

        col.set_cell_data_func(img, datafunc, None)
        col.set_visible(do_show)
        self.widget(menu).set_active(do_show)

        any_visible = any(
            [
                c.get_visible()
                for c in [self.netcol, self.diskcol, self.memcol, self.guestcpucol, self.hostcpucol]
            ]
        )
        self.spacer_txt.set_property("visible", not any_visible)

    def toggle_network_traffic_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_network_traffic_visible(),
            self.netcol,
            self.network_traffic_img,
            "menu_view_stats_network",
        )

    def toggle_disk_io_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_disk_io_visible(),
            self.diskcol,
            self.disk_io_img,
            "menu_view_stats_disk",
        )

    def toggle_memory_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_memory_usage_visible(),
            self.memcol,
            self.memory_usage_img,
            "menu_view_stats_memory",
        )

    def toggle_guest_cpu_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_guest_cpu_usage_visible(),
            self.guestcpucol,
            self.guest_cpu_usage_img,
            "menu_view_stats_guest_cpu",
        )

    def toggle_host_cpu_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_host_cpu_usage_visible(),
            self.hostcpucol,
            self.host_cpu_usage_img,
            "menu_view_stats_host_cpu",
        )

    def toggle_stats_visible(self, src, stats_id):
        visible = src.get_active()
        set_stats = {
            COL_GUEST_CPU: self.config.set_vmlist_guest_cpu_usage_visible,
            COL_HOST_CPU: self.config.set_vmlist_host_cpu_usage_visible,
            COL_MEM: self.config.set_vmlist_memory_usage_visible,
            COL_DISK: self.config.set_vmlist_disk_io_visible,
            COL_NETWORK: self.config.set_vmlist_network_traffic_visible,
        }
        set_stats[stats_id](visible)

    def toggle_stats_visible_guest_cpu(self, src):
        self.toggle_stats_visible(src, COL_GUEST_CPU)

    def toggle_stats_visible_host_cpu(self, src):
        self.toggle_stats_visible(src, COL_HOST_CPU)

    def toggle_stats_visible_memory_usage(self, src):
        self.toggle_stats_visible(src, COL_MEM)

    def toggle_stats_visible_disk(self, src):
        self.toggle_stats_visible(src, COL_DISK)

    def toggle_stats_visible_network(self, src):
        self.toggle_stats_visible(src, COL_NETWORK)

    def guest_cpu_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.guest_cpu_time_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def host_cpu_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.host_cpu_time_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def memory_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.stats_memory_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def disk_io_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        d1, d2 = obj.disk_io_vectors(GRAPH_LEN, self.max_disk_rate)
        data = [(x + y) / 2 for x, y in zip(d1, d2)]
        cell.set_property("data_array", data)

    def network_traffic_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        d1, d2 = obj.network_traffic_vectors(GRAPH_LEN, self.max_net_rate)
        data = [(x + y) / 2 for x, y in zip(d1, d2)]
        cell.set_property("data_array", data)
