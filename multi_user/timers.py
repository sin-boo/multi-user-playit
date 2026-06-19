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
import time
import bpy
from replication.constants import (FETCHED, RP_COMMON, STATE_ACTIVE,
                                   STATE_INITIAL, STATE_LOBBY, STATE_SYNCING,
                                   STATE_SRV_SYNC, STATE_WAITING, UP)
from replication.exception import NonAuthorizedOperationError
from replication.interface import session
from replication import porcelain

from . import utils
from .bl_types.bl_material import (
    maybe_finalize_node_trees,
    refresh_mesh_material_slots,
    repair_material_node_trees,
    reset_material_finalize_state,
)
from .bl_types.bl_image import reload_images_waiting_for_files
from .bl_types.bl_file import is_replicated_file
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
                utils.network_log_fetch_progress(key[0], key[1])
        else:
            SessionListenTimer._last_fetch_progress = (-1, -1)
        session.listen()


class ApplyTimer(Timer):
    _pending_assets_prev = False
    _logged_post_asset_summary = False
    _failed_apply_nodes: dict[str, tuple[int, float]] = {}
    _deferred_nodes: dict[str, tuple[int, float]] = {}
    _logged_construct_skips: set[str] = set()
    _had_deferred_objects = False
    _sync_hierarchy_done = False
    _material_graph_repair_done = False
    _material_slots_refreshed = False
    _logged_visibility_post_hierarchy = False
    _logged_visibility_post_repair = False
    _logged_host_scene_switch_skip = False
    _host_echo_promoted_total = 0
    _next_interval: float | None = None
    _MAX_APPLY_RETRIES = 3
    _MAX_HIERARCHY_RETRIES = 12
    # During the initial post-fetch sync, apply quickly instead of waiting for
    # depsgraph_update_rate between every small batch.
    _INITIAL_SYNC_BATCH_SIZE = 30
    _SYNC_TIME_BUDGET_S = 0.08

    def main(self):
        try:
            self._next_interval = None
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
                settings = utils.get_preferences()
                idle_rate = settings.depsgraph_update_rate if settings else 1.0
                return self._next_interval if self._next_interval is not None else idle_rate

    @classmethod
    def _schedule_defer(cls, node_uuid: str, now: float, base_delay: float = 0.5) -> None:
        attempts, _ = cls._deferred_nodes.get(node_uuid, (0, 0.0))
        attempts += 1
        delay = min(5.0, base_delay * attempts)
        cls._deferred_nodes[node_uuid] = (attempts, now + delay)
        cls._had_deferred_objects = True

    @classmethod
    def reset_sync_state(cls) -> None:
        cls._pending_assets_prev = False
        cls._logged_post_asset_summary = False
        cls._failed_apply_nodes.clear()
        cls._deferred_nodes.clear()
        cls._logged_construct_skips.clear()
        cls._had_deferred_objects = False
        cls._sync_hierarchy_done = False
        cls._material_graph_repair_done = False
        cls._material_slots_refreshed = False
        cls._logged_visibility_post_hierarchy = False
        cls._logged_visibility_post_repair = False
        cls._logged_host_scene_switch_skip = False
        cls._host_echo_promoted_total = 0

    def execute(self):
        if not session or session.state != STATE_ACTIVE:
            ApplyTimer.reset_sync_state()
            return

        utils.update_textures_fetch_on_shading_change(bpy.context)
        if not utils.textures_fetch_enabled(bpy.context):
            utils.enable_textures_fetch(bpy.context)

        settings = utils.get_preferences()
        batch_size = settings.apply_batch_size if settings else 10
        idle_rate = settings.depsgraph_update_rate if settings else 1.0
        burst_sync = utils.count_fetched_datablocks(session.repository) > 0
        if burst_sync:
            batch_size = max(batch_size, ApplyTimer._INITIAL_SYNC_BATCH_SIZE)
        sync_deadline = (
            time.monotonic() + ApplyTimer._SYNC_TIME_BUDGET_S
            if burst_sync else None
        )
        applied = 0
        pending_assets = utils.has_pending_fetched_assets(session.repository)

        initial_sync_active = not ApplyTimer._sync_hierarchy_done
        repaired_meshes = 0
        if not utils.is_session_host():
            repaired_meshes = utils.repair_incomplete_meshes(session.repository)
        if repaired_meshes:
            utils.network_log(
                logging.INFO,
                "Re-queued %s mesh(es) missing geometry",
                repaired_meshes,
            )
            for node_uuid in list(ApplyTimer._failed_apply_nodes.keys()):
                node = session.repository.graph.get(node_uuid)
                if (
                    node
                    and node.data
                    and node.data.get('type_id') in utils.HIERARCHY_TYPE_IDS
                ):
                    ApplyTimer._failed_apply_nodes.pop(node_uuid, None)

        candidates = []
        now = time.monotonic()
        index_ranks = utils.build_index_sorted_ranks(session.repository)
        material_assets_ready = not utils.has_pending_fetched_asset_type(
            session.repository,
            utils.LOAD_BEFORE_MATERIAL_TYPE_IDS,
        )
        for node in session.repository.graph.keys():
            node_ref = session.repository.graph.get(node)
            if node_ref is None or node_ref.state != FETCHED:
                continue
            type_id = node_ref.data.get('type_id') if node_ref.data else None
            if pending_assets and type_id not in utils.ASSET_TYPE_IDS:
                continue
            if type_id == 'Material' and not material_assets_ready:
                continue
            if type_id in utils.OBJECT_TYPE_IDS:
                if not utils.is_object_apply_ready(node_ref, session.repository):
                    defer_state = ApplyTimer._deferred_nodes.get(node_ref.uuid)
                    if defer_state and now < defer_state[1]:
                        continue
                    ApplyTimer._schedule_defer(
                        node_ref.uuid,
                        now,
                        base_delay=0.0 if burst_sync else 0.5,
                    )
                    continue
            defer_state = ApplyTimer._deferred_nodes.get(node_ref.uuid)
            if defer_state and now < defer_state[1]:
                continue
            retry_state = ApplyTimer._failed_apply_nodes.get(node_ref.uuid)
            if retry_state:
                failures, next_retry = retry_state
                max_retries = (
                    ApplyTimer._MAX_HIERARCHY_RETRIES
                    if type_id in utils.HIERARCHY_TYPE_IDS
                    else ApplyTimer._MAX_APPLY_RETRIES
                )
                if failures >= max_retries:
                    continue
                if now < next_retry:
                    continue
            candidates.append((
                utils.asset_apply_sort_key(type_id, node, index_ranks),
                node,
                type_id,
            ))

        candidates.sort(key=lambda item: item[0])

        for _, node, type_id in candidates:
            if applied >= batch_size:
                break
            if sync_deadline is not None and time.monotonic() >= sync_deadline:
                break

            node_ref = session.repository.graph.get(node)
            if node_ref is None or node_ref.instance is None:
                if node_ref and node_ref.data:
                    try:
                        node_ref.instance = session.repository.rdp.resolve(node_ref.data)
                        if node_ref.instance is None:
                            node_ref.instance = session.repository.rdp.construct(node_ref.data)
                        if node_ref.instance is not None and not is_replicated_file(node_ref.instance):
                            node_ref.instance.uuid = node_ref.uuid
                    except Exception:
                        logging.error(f"Fail to construct {node_ref.uuid}")
                        traceback.print_exc()
                        utils.network_log(
                            logging.ERROR,
                            "Failed to construct node %s (type=%s)",
                            node_ref.uuid,
                            type_id,
                        )
                if node_ref is None or node_ref.instance is None:
                    block_name = (
                        node_ref.data.get('name', '?')
                        if node_ref and node_ref.data else '?'
                    )
                    if node_ref and node_ref.uuid not in ApplyTimer._logged_construct_skips:
                        ApplyTimer._logged_construct_skips.add(node_ref.uuid)
                        utils.network_log(
                            logging.WARNING,
                            "Deferring apply: no instance for %s type=%s name=%s",
                            getattr(node_ref, 'uuid', node),
                            type_id,
                            block_name,
                        )
                    if node_ref:
                        ApplyTimer._schedule_defer(
                            node_ref.uuid,
                            now,
                            base_delay=0.0 if burst_sync else 0.5,
                        )
                    continue

            block_name = node_ref.data.get('name', '?') if node_ref.data else '?'

            if utils.should_skip_host_echo_apply(node_ref, initial_sync_active):
                utils.promote_fetched_node_to_up(node_ref)
                ApplyTimer._host_echo_promoted_total += 1
                ApplyTimer._failed_apply_nodes.pop(node_ref.uuid, None)
                ApplyTimer._deferred_nodes.pop(node_ref.uuid, None)
                applied += 1
                continue

            utils.network_log(
                logging.INFO,
                "Applying %s %r (uuid=%s)",
                type_id or 'unknown',
                block_name,
                node_ref.uuid,
            )

            try:
                shared_data.session.applied_updates.append(node)
                porcelain.apply(session.repository, node)
            except Exception:
                failures, _ = ApplyTimer._failed_apply_nodes.get(node_ref.uuid, (0, 0.0))
                failures += 1
                if burst_sync:
                    retry_delay = min(2.0, 0.25 * failures)
                else:
                    retry_delay = min(30.0, 2.0 ** failures)
                ApplyTimer._failed_apply_nodes[node_ref.uuid] = (
                    failures,
                    time.monotonic() + retry_delay,
                )
                max_retries = (
                    ApplyTimer._MAX_HIERARCHY_RETRIES
                    if type_id in utils.HIERARCHY_TYPE_IDS
                    else ApplyTimer._MAX_APPLY_RETRIES
                )
                logging.exception("Fail to apply %s", node_ref.uuid)
                utils.network_log(
                    logging.ERROR,
                    "Failed to apply %s type=%s name=%r (attempt %s/%s, retry in %.0fs)",
                    node_ref.uuid,
                    type_id,
                    block_name,
                    failures,
                    max_retries,
                    retry_delay,
                )
            else:
                ApplyTimer._failed_apply_nodes.pop(node_ref.uuid, None)
                ApplyTimer._deferred_nodes.pop(node_ref.uuid, None)
                applied += 1
                if type_id in utils.OBJECT_TYPE_IDS and node_ref.instance is not None:
                    utils.log_object_apply_diagnostics(
                        node_ref.instance,
                        label=f"after apply {block_name!r}",
                    )
                if type_id == 'ShaderNodeTree' and not ApplyTimer._sync_hierarchy_done:
                    reset_material_finalize_state()
                impl = session.repository.rdp.get_implementation(node_ref.instance)
                # During the first full sync, datablocks are applied in dependency
                # order already. Parent/child force-reloads only duplicate work and
                # trigger redundant material/texture reload cascades.
                if ApplyTimer._sync_hierarchy_done and impl.bl_reload_parent:
                    for parent in session.repository.graph.get_parents(node):
                        logging.debug("Refresh parent {node}")
                        try:
                            # Record the forced reload so the depsgraph handler
                            # ignores this self-induced change instead of
                            # committing/pushing it back (which echoes from the
                            # server and re-fetches the node forever).
                            shared_data.session.applied_updates.append(parent.uuid)
                            porcelain.apply(
                                session.repository,
                                parent.uuid,
                                force=True
                            )
                        except Exception:
                            logging.error(f"Fail to refresh parent {parent.uuid}")
                            traceback.print_exc()
                if (
                    ApplyTimer._sync_hierarchy_done
                    and hasattr(impl, 'bl_reload_child')
                    and impl.bl_reload_child
                ):
                    for dep in node_ref.dependencies:
                        dep_node = session.repository.graph.get(dep)
                        if dep_node and dep_node.state == UP and dep_node.data:
                            dep_type = dep_node.data.get('type_id')
                            if dep_type in utils.FILE_ASSET_TYPE_IDS | {'Image'}:
                                continue
                        try:
                            # See note above: guard forced child reloads too so
                            # they don't trigger a commit/push feedback loop.
                            shared_data.session.applied_updates.append(dep)
                            porcelain.apply(session.repository,
                                            dep,
                                            force=True)
                        except Exception:
                            logging.error(f"Fail to refresh child {dep}")
                            traceback.print_exc()

        pending_assets_after = utils.has_pending_fetched_assets(session.repository)

        if applied:
            remaining = sum(
                1 for node in session.repository.graph.values()
                if node.state == FETCHED
            )
            utils.network_log(
                logging.INFO,
                "Applied %s datablock(s), %s FETCHED remaining, pending_assets=%s",
                applied,
                remaining,
                pending_assets_after,
            )

        if pending_assets and not pending_assets_after:
            reloaded = reload_images_waiting_for_files(session.repository)
            if reloaded:
                utils.network_log(
                    logging.INFO,
                    "Reloaded %s image(s) after texture files synced",
                    reloaded,
                )
            refreshed = refresh_mesh_material_slots(session.repository)
            if refreshed:
                utils.network_log(
                    logging.INFO,
                    "Refreshed material slots on %s mesh(es)",
                    refreshed,
                )
            utils.log_replication_graph_summary(
                session.repository,
                label="post-asset-sync",
            )
            ApplyTimer._logged_post_asset_summary = True

        if (
            not pending_assets_after
            and not ApplyTimer._logged_post_asset_summary
            and applied
        ):
            utils.log_replication_graph_summary(
                session.repository,
                label="post-apply",
            )

        ApplyTimer._pending_assets_prev = pending_assets_after

        try:
            maybe_finalize_node_trees(session.repository)
        except Exception as exc:
            logging.exception("Failed to finalize node trees")
            utils.network_log(
                logging.ERROR,
                "maybe_finalize_node_trees failed: %s: %s",
                type(exc).__name__,
                exc,
            )

        initial_sync_pending = utils.has_pending_initial_sync_datablocks(
            session.repository,
        )

        if not pending_assets_after and not initial_sync_pending:
            if applied and not ApplyTimer._logged_post_asset_summary:
                refreshed = refresh_mesh_material_slots(session.repository)
                if refreshed:
                    utils.network_log(
                        logging.INFO,
                        "Refreshed material slots on %s mesh(es) after finalize",
                        refreshed,
                    )
            reloaded = reload_images_waiting_for_files(session.repository)
            if reloaded:
                utils.network_log(
                    logging.INFO,
                    "Reloaded %s image(s) after finalize",
                    reloaded,
                )

        # Once assets are synced, keep linking collections/scenes so objects
        # become visible even if a few datablocks are still FETCHED.
        if (
            not pending_assets_after
            and not initial_sync_pending
            and not ApplyTimer._sync_hierarchy_done
        ):
            if utils.is_session_host():
                ApplyTimer._sync_hierarchy_done = True
                if ApplyTimer._host_echo_promoted_total:
                    utils.network_log(
                        logging.INFO,
                        "Host initial sync: promoted %s local datablock(s) without "
                        "destructive re-apply",
                        ApplyTimer._host_echo_promoted_total,
                    )
                utils.network_log(
                    logging.INFO,
                    "Host: skipped hierarchy force-reapply (local scene is authoritative)",
                )
            else:
                try:
                    utils.refresh_scene_hierarchy(session.repository)
                except Exception:
                    logging.exception("Failed to refresh scene hierarchy")

            has_linked_scene = any(
                node.data
                and node.data.get('type_id') == 'Scene'
                and isinstance(node.instance, bpy.types.Scene)
                and utils.count_scene_linked_objects(node.instance) > 0
                for node in session.repository.graph.values()
            )
            if has_linked_scene and not ApplyTimer._sync_hierarchy_done:
                ApplyTimer._sync_hierarchy_done = True
                utils.network_log(logging.INFO, "Initial scene hierarchy linked")
            if ApplyTimer._sync_hierarchy_done and not ApplyTimer._logged_visibility_post_hierarchy:
                utils.log_scene_visibility_audit(
                    "post-hierarchy",
                    session.repository,
                )
                ApplyTimer._logged_visibility_post_hierarchy = True

        if (
            ApplyTimer._sync_hierarchy_done
            and not ApplyTimer._material_graph_repair_done
            and not pending_assets_after
            and not initial_sync_pending
        ):
            try:
                repair_stats = repair_material_node_trees(session.repository)
                utils.network_log(
                    logging.INFO,
                    "Repaired material node trees: %s material(s) (%s rebuilt, %s failed), "
                    "%s image ref(s), %s group ref(s), %s links wired",
                    repair_stats['materials'],
                    repair_stats['rebuilt'],
                    repair_stats['failed'],
                    repair_stats['images_rebound'],
                    repair_stats['groups_rebound'],
                    repair_stats['links_wired'],
                )
            except Exception as exc:
                logging.exception("Material graph repair pass failed")
                utils.network_log(
                    logging.ERROR,
                    "Material graph repair pass failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            finally:
                ApplyTimer._material_graph_repair_done = True
            refreshed = refresh_mesh_material_slots(session.repository)
            if refreshed:
                utils.network_log(
                    logging.INFO,
                    "Refreshed material slots on %s mesh(es) after graph repair",
                    refreshed,
                )
            else:
                utils.network_log(
                    logging.INFO,
                    "Material slot refresh after graph repair: no mesh slot changes",
                )
            if not ApplyTimer._logged_visibility_post_repair:
                utils.log_scene_visibility_audit(
                    "post-material-repair",
                    session.repository,
                )
                utils.log_warning_error_summary("post-material-repair")
                ApplyTimer._logged_visibility_post_repair = True
            ApplyTimer._material_slots_refreshed = True

        if (
            ApplyTimer._sync_hierarchy_done
            and not ApplyTimer._material_slots_refreshed
            and not pending_assets_after
            and not initial_sync_pending
        ):
            refreshed = refresh_mesh_material_slots(session.repository)
            if refreshed:
                utils.network_log(
                    logging.INFO,
                    "Refreshed material slots on %s mesh(es) after initial sync",
                    refreshed,
                )
            ApplyTimer._material_slots_refreshed = True

        if not pending_assets_after and not initial_sync_pending:
            runtime = getattr(bpy.context.window_manager, 'session', None)
            is_host = utils.is_session_host()
            if is_host:
                if not ApplyTimer._logged_host_scene_switch_skip:
                    ApplyTimer._logged_host_scene_switch_skip = True
                    utils.network_log(
                        logging.DEBUG,
                        "client scene switch skipped (host session)",
                    )
            else:
                if utils.get_connected_session_info().get('is_host'):
                    utils.network_log(
                        logging.WARNING,
                        "client scene switch path active while connected_session_info "
                        "says host (wm.session.is_host=False)",
                    )
                try:
                    active = bpy.context.window.scene
                    needs_switch = (
                        not shared_data.session.client_scene_switched
                        or (active is not None and utils.count_scene_linked_objects(active) == 0)
                    )
                    if not needs_switch:
                        utils.network_log(
                            logging.DEBUG,
                            "client scene switch not needed (scene=%r linked=%s switched=%s)",
                            active.name if active else None,
                            utils.count_scene_linked_objects(active) if active else 0,
                            shared_data.session.client_scene_switched,
                        )
                    elif needs_switch and utils.switch_client_to_host_scene(
                        session.repository,
                    ):
                        shared_data.session.client_scene_switched = True
                        utils.network_log(
                            logging.INFO,
                            "Switched client view to the synced host scene",
                        )
                        utils.log_scene_visibility_audit(
                            "post-client-scene-switch",
                            session.repository,
                        )
                    elif needs_switch:
                        utils.network_log(
                            logging.WARNING,
                            "client scene switch needed but switch_client_to_host_scene "
                            "returned False (active=%r linked=%s)",
                            active.name if active else None,
                            utils.count_scene_linked_objects(active) if active else 0,
                        )
                except Exception:
                    logging.exception("Failed to switch client to host scene")
                    utils.network_log(
                        logging.ERROR,
                        "Failed to switch client to host scene — see traceback in log file",
                    )

        remaining_fetched = utils.count_fetched_datablocks(session.repository)
        more_candidates = len(candidates) > applied
        if remaining_fetched > 0 or (burst_sync and (applied > 0 or more_candidates)):
            self._next_interval = 0.0
        else:
            self._next_interval = idle_rate


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
    _fast_refresh = False

    def __init__(self, timeout=1):
        super().__init__(timeout)

    def execute(self):
        refresh_sidebar_view()
        utils.update_textures_fetch_on_shading_change(bpy.context)
        textures_enabled = utils.textures_fetch_enabled(bpy.context)
        applied, total = utils.get_asset_sync_progress()
        scene_applied, scene_total = utils.get_scene_apply_progress()
        pending_assets = textures_enabled and total > 0 and applied < total
        pending_scene = scene_total > 0 and scene_applied < scene_total
        pending = pending_assets or pending_scene
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
