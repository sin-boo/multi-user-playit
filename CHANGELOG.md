# Changelog

All notable changes to this project will be documented in this file.

## [0.7.5] - 2026-03-18

- fix geometry nodes input

## [0.7.1] - 2026-03-17

- fix color ramp support

## [0.6.8] - 2025-09-29

- Blender 4 compatibility

## [0.0.2] - 2020-02-28

### Added

- Blender animation features support (alpha).
  - Action.
  - Armature (Unstable).
  - Shape key.
  - Drivers.
  - Constraints.
- Snap to user timeline tool.
- Light probes support (only since 2.83).
- Metaballs support.
- Improved modifiers support.
- Online documentation.
- Improved Undo handling.
- Improved overall session handling:
  - Time To Leave : ensure clients/server disconnect automatically on connection lost.
  - Ping: show clients latency.
  - Non-blocking connection.
  - Connection state tracking.
- Service communication layer to manage background daemons.

### Changed

- UI revamp:
  - Show users frame.
  - Expose IPC(inter process communication) port.
  - New user list.
  - Progress bar to track connection status.
- Right management takes view-layer in account for object selection.
- Use a basic BFS approach for replication graph pre-load.
- Serialization is now based on marshal (2x performance improvements).
- Let pip chose python dependencies install path.

## [0.0.3] - 2020-07-29

### Added

- Auto updater support
- Big Performances improvements on Meshes, Gpencils, Actions
- Multi-scene workflow support
- Render setting synchronization
- Kick command
- Dedicated server with a basic command set
- Administrator session status
- Tests
- Blender 2.83-2.90 support

### Changed

- Config is now stored in blender user preference
- Documentation update
- Connection protocol
- UI revamp:
  - user localization
  - repository init

### Removed

- Unused strict right management strategy
- Legacy config management system

## [0.1.0] - 2020-10-05

### Added

- Dependency graph driven updates [experimental]
- Edit Mode updates
- Late join mechanism 
- Sync Axis lock replication
- Sync collection offset
- Sync camera  orthographic scale
- Sync custom fonts
- Sync sound files
- Logging configuration (file output and level)
- Object visibility type replication
- Optionnal sync for active camera
- Curve->Mesh conversion
- Mesh->gpencil conversion

### Changed

- Auto updater now handle installation from branches
- Use uuid for collection loading
- Moved session instance to replication package

### Fixed

- Prevent unsupported data types to crash the session
- Modifier vertex group assignation
- World sync
- Snapshot UUID error
- The world is not synchronized

## [0.1.1] - 2020-10-16

### Added

- Session status widget
- Affect dependencies during change owner
- Dedicated server managment scripts(@brybalicious)

### Changed

- Refactored presence.py
- Reset button UI icon 
- Documentation `How to contribute` improvements (@brybalicious)
- Documentation `Hosting guide` improvements (@brybalicious)
- Show flags are now available from the viewport overlay
 
### Fixed

- Render sync race condition (causing scene errors)
- Binary differentials
- Hybrid session crashes between Linux/Windows
- Materials node default output value
- Right selection
- Client node rights changed to COMMON after disconnecting from the server 
- Collection instances selection draw
- Packed image save error
- Material replication
- UI spelling errors (@brybalicious)


## [0.2.0] - 2020-12-17

### Added

- Documentation `Troubleshouting` section (@brybalicious)
- Documentation `Update` section (@brybalicious)
- Documentation `Cloud Hosting Walkthrough` (@brybalicious)
- Support DNS name
- Sync annotations
- Sync volume objects
- Sync material node_goups 
- Sync VSE
- Sync grease pencil modifiers
- Sync textures (modifier only)
- Session status widget
- Disconnection popup 
- Popup with disconnection reason 


### Changed

- Improved GPencil performances

### Fixed

- Texture paint update
- Various documentation fixes section (@brybalicious)
- Empty and Light object selection highlights
- Material renaming
- Default material nodes input parameters
- blender 2.91 python api compatibility

## [0.3.0] - 2021-04-14

### Added

- Curve material support
- Cycle visibility settings
- Session save/load  operator 
- Add new scene support
- Physic initial support
- Geometry node initial support
- Blender 2.93 compatibility
### Changed

- Host documentation on Gitlab Page
- Event driven update (from the blender deps graph)

### Fixed

- Vertex group assignation
- Parent relation can't be removed
- Separate object
- Delete animation
- Sync missing holdout option for grease pencil material
- Sync missing `skin_vertices`  
- Exception access violation during Undo/Redo
- Sync missing armature bone Roll
- Sync missing driver data_path
- Constraint replication

## [0.4.0] - 2021-07-20

### Added

- Connection preset system (@Kysios)
- Display connected users active mode (users pannel and viewport) (@Kysios)
- Delta-based replication
- Sync timeline marker
- Sync images settings (@Kysios)
- Sync parent relation type (@Kysios)
- Sync uv project modifier
- Sync FCurves modifiers

### Changed

- User selection optimizations (draw and sync) (@Kysios)
- Improved shapekey syncing performances
- Improved gpencil syncing performances
- Integrate replication as a submodule
- The dependencies are now installed in a folder(blender addon folder) that no longer requires administrative rights
- Presence overlay UI optimization (@Kysios)

### Fixed

- User selection bounding box glitches for non-mesh objects (@Kysios)
- Transforms replication for animated objects
- GPencil fill stroke
- Sculpt and GPencil brushes deleted when joining a session (@Kysios)
- Auto-updater doesn't work for master and develop builds

## [0.5.0] - 2022-02-10

### Added

- New overall UI and UX (@Kysios)
- Documentation overall update (@Kysios)
- Server presets (@Kysios)
- Server online status (@Kysios)
- Draw connected user color in the user list
- Private session (access protected with a password) (@Kysios)

### Changed

- Dependencies are now installed in the addon folder and correctly cleaned during the addon removal process

### Fixed

- Python 3.10 compatibility (@notfood)
- Blender 3.x compatibility
- Skin vertex radius synchronization (@kromar)
- Sequencer audio strip synchronization
- Crash with empty after a reconnection

## [0.5.1] - 2022-02-10

### Fixed

- Auto updater breaks dependency auto installer
- Auto updater update from tag

## [0.5.2] - 2022-02-18

### Fixed

- Objects not selectable after user leaves session
- Geometry nodes attribute toogle doesn't sync

## [0.5.3] - 2022-03-11

### Changed

- Snapshots logs
### Fixed

- Server crashing during snapshots
- Blender 3.1 numpy loading error during early connection process
- Server docker arguments

## [0.5.5] - 2022-06-12

### Fixed

- Numpy mesh serialization error