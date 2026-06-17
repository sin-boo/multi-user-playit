# ##### BEGIN GPL LICENSE BLOCK #####
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####

import logging
import sys
import traceback
import bpy
from replication.constants import (FETCHED, RP_COMMON, STATE_ACTIVE,
                                   STATE_INITIAL, STATE_LOBBY, STATE_SYNCING,
                                   STATE_SRV_SYNC, STATE_WAITING)
from replication.exception import NonAuthorizedOperationError
from replication.interface import session
from replication import porcelain

from . import utils
from .bl_types.bl_material import maybe_finalize_node_trees
from .presence import (UserFrustumWidget, UserNameWidget, UserModeWidget, UserSelectionWidget,
                       generate_user_camera, get_view_matrix, refresh_3d_view,
                       refresh_sidebar_view, presence_viewer)

from . import shared_data


# Registered timers
timers_registry = dict()


def is_annotating(context: bpy.types.Context):
    """ Check if the annotate mode is enabled
    """
    active_tool = bpy.context.workspace.tools.from_space_view3d_mode('OBJECT', create=False)
    return (active_tool and active_tool.idname == 'builtin.annotate')


class Timer(object):
    """Timer binder interface for blender

    Run a bpy.app.Timer in the background looping at the given rate
    """

    disconnect_on_error = False

    def __init__(self, timeout=10, id=None):
        self._timeout = timeout
        self.is_running = False
        self.id = id if id else self.__class__.__name__

    def register(self):
        """Register the timer into the blender timer system
        """

        if not self.is_running:
            timers_registry[self.id] = self
            bpy.app.timers.register(self.main)
            self.is_running = True
            logging.debug(f"Register {self.__class__.__name__}")
        else:
            logging.debug(
                f"Timer {self.__class__.__name__} already registered")

    def main(self):
        try:
            self.execute()
        except Exception as e:
            logging.error(e)
            traceback.print_exc()
            utils.network_log(
                logging.ERROR,
                "Timer %s failed: %s: %s",
                self.id,
                type(e).__name__,
                e,
            )
            utils.network_log(logging.ERROR, traceback.format_exc())
            self.unregister()
            if self.disconnect_on_error and session.state != STATE_INITIAL:
                session.disconnect(
                    reason=f"Timer {self.id}: {type(e).__name__}: {e}"
                )
        else:
            if self.is_running:
                return self._timeout

    def execute(self):
        """Main timer loop
        """
        raise NotImplementedError

    def unregister(self):
        """Unnegister the timer of the blender timer system
        """
        if bpy.app.timers.is_registered(self.main):
            logging.info(f"Unregistering {self.id}")
            bpy.app.timers.unregister(self.main)

        del timers_registry[self.id]
        self.is_running = False


class SessionBackupTimer(Timer):
    def __init__(self, timeout=10, filepath=None):
        self._filepath = filepath
        super().__init__(timeout)

    def execute(self):
        session.repository.dumps(self._filepath)


class SessionListenTimer(Timer):
    _last_session_state = STATE_INITIAL
    _last_fetch_progress = (-1, -1)
    disconnect_on_error = True

    def execute(self):
        current_state = session.state
        if current_state != SessionListenTimer._last_session_state:
            utils.log_session_state_change(
                SessionListenTimer._last_session_state,
                current_state,
            )
            SessionListenTimer._last_session_state = current_state
        if current_state in (STATE_SYNCING, STATE_SRV_SYNC, STATE_WAITING):
            progress = session.state_progress
            key = (progress.get('current', -1), progress.get('total', -1))
            if key != SessionListenTimer._last_fetch_progress:
                SessionListenTimer._last_fetch_progress = key
                utils.network_log(
                    logging.INFO,
                    "fetch progress %s/%s",
                    key[0],
                    key[1],
                )
        session.listen()


class ApplyTimer(Timer):
    def execute(self):
        if not session or session.state != STATE_ACTIVE:
            return

        utils.update_textures_fetch_on_shading_change(bpy.context)
        textures_enabled = utils.textures_fetch_enabled(bpy.context)

        settings = utils.get_preferences()
        batch_size = settings.apply_batch_size if settings else 10
        applied = 0

        candidates = []
        for node in session.repository.graph.keys():
            node_ref = session.repository.graph.get(node)
            if node_ref is None or node_ref.state != FETCHED:
                continue
            type_id = node_ref.data.get('type_id') if node_ref.data else None
            if utils.is_deferred_asset_type(type_id) and not textures_enabled:
                continue
            candidates.append((type_id in utils.ASSET_TYPE_IDS, node))

        candidates.sort(key=lambda item: item[0])

        for _, node in candidates:
            if applied >= batch_size:
                break

            node_ref = session.repository.graph.get(node)
            if node_ref is None or node_ref.instance is None:
                if node_ref and node_ref.data:
                    node_ref.instance = session.repository.rdp.resolve(node_ref.data)
                    if node_ref.instance is None:
                        node_ref.instance = session.repository.rdp.construct(node_ref.data)
                    if node_ref.instance is not None:
                        node_ref.instance.uuid = node_ref.uuid
                if node_ref is None or node_ref.instance is None:
                    continue

            try:
                shared_data.session.applied_updates.append(node)
                porcelain.apply(session.repository, node)
            except Exception:
                logging.error(f"Fail to apply {node_ref.uuid}")
                traceback.print_exc()
                utils.network_log(
                    logging.ERROR,
                    "Failed to apply node %s",
                    node_ref.uuid,
                )
            else:
                applied += 1
                impl = session.repository.rdp.get_implementation(node_ref.instance)
                if impl.bl_reload_parent:
                    for parent in session.repository.graph.get_parents(node):
                        logging.debug("Refresh parent {node}")
                        try:
                            porcelain.apply(
                                session.repository,
                                parent.uuid,
                                force=True
                            )
                        except Exception:
                            logging.error(f"Fail to refresh parent {parent.uuid}")
                            traceback.print_exc()
                if hasattr(impl, 'bl_reload_child') and impl.bl_reload_child:
                    for dep in node_ref.dependencies:
                        try:
                            porcelain.apply(session.repository,
                                            dep,
                                            force=True)
                        except Exception:
                            logging.error(f"Fail to refresh child {dep}")
                            traceback.print_exc()

        if applied:
            remaining = sum(
                1 for node in session.repository.graph.values()
                if node.state == FETCHED
            )
            utils.network_log(
                logging.DEBUG,
                "Applied %s datablock(s), %s remaining",
                applied,
                remaining,
            )

        try:
            maybe_finalize_node_trees(session.repository, textures_enabled=textures_enabled)
        except Exception:
            logging.error("Failed to finalize node trees")
            traceback.print_exc()


class AnnotationUpdates(Timer):
    def __init__(self, timeout=1):
        self._annotating = False
        self._settings = utils.get_preferences()

        super().__init__(timeout)

    def execute(self):
        if session and session.state == STATE_ACTIVE:
            ctx = bpy.context
            annotation_gp = ctx.scene.grease_pencil

            if annotation_gp and not annotation_gp.uuid:
                ctx.scene.update_tag()

            # if an annotation exist and is tracked
            if annotation_gp and annotation_gp.uuid:
                registered_gp = session.repository.graph.get(annotation_gp.uuid)
                if is_annotating(bpy.context):
                    # try to get the right on it
                    if registered_gp.owner == RP_COMMON:
                        self._annotating = True
                        logging.debug(
                            "Getting the right on the annotation GP")
                        porcelain.lock(
                            session.repository,
                            [registered_gp.uuid],
                            ignore_warnings=True,
                            affect_dependencies=False
                        )

                    if registered_gp.owner == self._settings.username:
                        porcelain.commit(session.repository, annotation_gp.uuid)
                        porcelain.push(session.repository, 'origin', annotation_gp.uuid)

                elif self._annotating:
                    porcelain.unlock(
                        session.repository,
                        [registered_gp.uuid],
                        ignore_warnings=True,
                        affect_dependencies=False
                    )
                    self._annotating = False


class DynamicRightSelectTimer(Timer):
    def __init__(self, timeout=.1):
        super().__init__(timeout)
        self._last_selection = set()
        self._user = None

    def execute(self):
        settings = utils.get_preferences()

        if not session or session.state != STATE_ACTIVE:
            return

        if self._user is None:
            self._user = session.online_users.get(settings.username)

        if self._user:
            current_selection = set(utils.get_selected_objects(
                bpy.context.scene,
                bpy.data.window_managers['WinMan'].windows[0].view_layer
            ))
            if current_selection != self._last_selection:
                to_lock = list(current_selection.difference(self._last_selection))
                to_release = list(self._last_selection.difference(current_selection))
                instances_to_lock = list()

                for node_id in to_lock:
                    node = session.repository.graph.get(node_id)
                    if node and hasattr(node, 'data'):
                        instance_mode = node.data.get('instance_type')
                        if instance_mode and instance_mode == 'COLLECTION':
                            to_lock.remove(node_id)
                            instances_to_lock.append(node_id)
                if instances_to_lock:
                    try:
                        porcelain.lock(
                            session.repository,
                            instances_to_lock,
                            ignore_warnings=True,
                            affect_dependencies=False,
                        )
                    except NonAuthorizedOperationError as e:
                        logging.warning(e)

                if to_release:
                    try:
                        porcelain.unlock(
                            session.repository,
                            to_release,
                            ignore_warnings=True,
                            affect_dependencies=True,
                        )
                    except NonAuthorizedOperationError as e:
                        logging.warning(e)
                if to_lock:
                    try:
                        porcelain.lock(
                            session.repository,
                            to_lock,
                            ignore_warnings=True,
                            affect_dependencies=True,
                        )
                    except NonAuthorizedOperationError as e:
                        logging.warning(e)

                self._last_selection = current_selection

                user_metadata = {
                    'selected_objects': current_selection
                }

                porcelain.update_user_metadata(session.repository, user_metadata)
                logging.debug("Update selection")

                if len(current_selection) == 0:
                    owned_keys = [
                        k
                        for k, v in session.repository.graph.items()
                        if v.owner == settings.username
                    ]
                    if owned_keys:
                        try:
                            porcelain.unlock(
                                session.repository,
                                owned_keys,
                                ignore_warnings=True,
                                affect_dependencies=True,
                            )
                        except NonAuthorizedOperationError as e:
                            logging.warning(e)

        for obj in bpy.data.objects:
            object_uuid = getattr(obj, 'uuid', None)
            if object_uuid:
                is_selectable = not session.repository.is_node_readonly(object_uuid)
                if obj.hide_select != is_selectable:
                    obj.hide_select = is_selectable
                    shared_data.session.applied_updates.append(object_uuid)


class ClientUpdate(Timer):
    def __init__(self, timeout=.1):
        super().__init__(timeout)
        self.handle_quit = False
        self.users_metadata = {}

    def execute(self):
        settings = utils.get_preferences()

        if not session or not presence_viewer:
            return
        if session.state not in [STATE_ACTIVE, STATE_LOBBY]:
            return

        if session and presence_viewer:
            if session.state in [STATE_ACTIVE, STATE_LOBBY]:
                local_user = session.online_users.get(
                    settings.username)

                if not local_user:
                    return
                else:
                    for username, user_data in session.online_users.items():
                        if username != settings.username:
                            cached_user_data = self.users_metadata.get(
                                username)
                            new_user_data = session.online_users[username]['metadata']

                            if cached_user_data is None:
                                self.users_metadata[username] = user_data['metadata']
                            elif 'view_matrix' in cached_user_data and 'view_matrix' in new_user_data and cached_user_data['view_matrix'] != new_user_data['view_matrix']:
                                refresh_3d_view()
                                self.users_metadata[username] = user_data['metadata']
                                break
                            else:
                                self.users_metadata[username] = user_data['metadata']

                local_user_metadata = local_user.get('metadata')
                scene_current = bpy.context.scene.name
                local_user = session.online_users.get(settings.username)
                current_view_corners = generate_user_camera()

                # Init client metadata
                if not local_user_metadata or 'color' not in local_user_metadata.keys():
                    metadata = {
                        'view_corners': get_view_matrix(),
                        'view_matrix': get_view_matrix(),
                        'color': (settings.client_color.r,
                                  settings.client_color.g,
                                  settings.client_color.b,
                                  1),
                        'frame_current': bpy.context.scene.frame_current,
                        'scene_current': scene_current,
                        'mode_current': bpy.context.mode
                    }
                    porcelain.update_user_metadata(session.repository, metadata)

                # Update client representation
                # Update client current scene
                elif scene_current != local_user_metadata['scene_current']:
                    local_user_metadata['scene_current'] = scene_current
                    porcelain.update_user_metadata(session.repository, local_user_metadata)
                elif 'view_corners' in local_user_metadata and current_view_corners != local_user_metadata['view_corners']:
                    local_user_metadata['view_corners'] = current_view_corners
                    local_user_metadata['view_matrix'] = get_view_matrix(
                    )
                    porcelain.update_user_metadata(session.repository, local_user_metadata)
                elif bpy.context.mode != local_user_metadata['mode_current']:
                    local_user_metadata['mode_current'] = bpy.context.mode
                    porcelain.update_user_metadata(session.repository, local_user_metadata)


class SessionStatusUpdate(Timer):
    _last_asset_progress = (-1, -1, False)
    _last_shading_active = False
    _fast_refresh = False

    def __init__(self, timeout=1):
        super().__init__(timeout)

    def execute(self):
        refresh_sidebar_view()

        utils.update_textures_fetch_on_shading_change(bpy.context)
        textures_enabled = utils.textures_fetch_enabled(bpy.context)
        applied, total = utils.get_asset_sync_progress()
        shading_active = utils.is_texture_shading_active(bpy.context)
        progress_key = (applied, total, textures_enabled)

        if progress_key != SessionStatusUpdate._last_asset_progress or \
                shading_active != SessionStatusUpdate._last_shading_active:
            SessionStatusUpdate._last_asset_progress = progress_key
            SessionStatusUpdate._last_shading_active = shading_active
            refresh_3d_view()

        pending_assets = textures_enabled and total > 0 and applied < total
        pending = pending_assets
        if pending and not SessionStatusUpdate._fast_refresh:
            SessionStatusUpdate._fast_refresh = True
            self._timeout = 0.25
        elif not pending and SessionStatusUpdate._fast_refresh:
            SessionStatusUpdate._fast_refresh = False
            self._timeout = 1


class SessionUserSync(Timer):
    def __init__(self, timeout=1):
        super().__init__(timeout)
        self.settings = utils.get_preferences()

    def execute(self):
        if not session or not presence_viewer:
            return
        if session.state not in (STATE_ACTIVE, STATE_LOBBY):
            return

        session_users = session.online_users
        ui_users = bpy.context.window_manager.online_users

        for index, user in enumerate(ui_users):
            if user.username not in session_users.keys() and \
                    user.username != self.settings.username:
                presence_viewer.remove_widget(f"{user.username}_cam")
                presence_viewer.remove_widget(f"{user.username}_select")
                presence_viewer.remove_widget(f"{user.username}_name")
                presence_viewer.remove_widget(f"{user.username}_mode")
                ui_users.remove(index)
                break

        for user in session_users:
            if user not in ui_users:
                new_key = ui_users.add()
                new_key.name = user
                new_key.username = user
                if user != self.settings.username:
                    presence_viewer.add_widget(
                        f"{user}_cam", UserFrustumWidget(user))
                    presence_viewer.add_widget(
                        f"{user}_select", UserSelectionWidget(user))
                    presence_viewer.add_widget(
                        f"{user}_name", UserNameWidget(user))
                    presence_viewer.add_widget(
                        f"{user}_mode", UserModeWidget(user))
