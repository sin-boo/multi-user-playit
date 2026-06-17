# MULTI-USER for Blender (playit.gg edition)

> Enable real-time collaborative workflow inside Blender — simplified hosting with [playit.gg](https://playit.gg)

<img src="https://i.imgur.com/X0B7O1Q.gif" width=600>

:warning: Under development, use it at your own risks. Currently tested on Windows platform. :warning:

This is a modified version of [Multi-User](https://gitlab.com/slumber/multi-user) by Swann Martinez, optimized for hosting over the internet with playit.gg. No port forwarding or manual firewall setup required — create a TCP tunnel, host in Blender, and share the tunnel address for others to join.

Licensed under the same [GNU GPL v3](LICENSE) as the original project.

## playit.gg quick start

See [scripts/playit_tunnel/SETUP.txt](scripts/playit_tunnel/SETUP.txt) for the full walkthrough. Summary:

1. **Host:** Run the playit.gg agent and create a **TCP** tunnel with **port count 3**, local IP `127.0.0.1`, local port matching Blender's host port (default `5555`).
2. **Host:** In Blender, Multi-User panel → set Port → **Host**.
3. **Join:** Add a server preset and paste the full playit address (e.g. `my-tunnel.gl.at.ply.gg:41234`) into the IP field — the port is parsed automatically.

The addon uses three consecutive TCP ports: base (commands), base+1 (data), base+2 (TTL).

This tool aims to allow multiple users to work on the same scene over the network. Based on a Clients / Server architecture, the data-oriented replication schema replicate blender data-blocks across the wire.

## Quick installation

1. Download [latest build](https://gitlab.com/slumber/multi-user/-/jobs/artifacts/develop/download?job=build) or [stable build](https://gitlab.com/slumber/multi-user/-/jobs/artifacts/master/download?job=build).
2. Install last_version.zip from your addon preferences.

[Dependencies](#dependencies) will be automatically added to your blender python during installation.

## Usage

See the [documentation](https://slumber.gitlab.io/multi-user/index.html) for details.

## Troubleshooting

See the [troubleshooting guide](https://slumber.gitlab.io/multi-user/getting_started/troubleshooting.html) for tips on the most common issues.

## Current development status

Currently, not all data-block are supported for replication over the wire. The following list summarizes the status for each ones.

| Name           | Status |                                 Comment                                 |
| -------------- | :----: | :---------------------------------------------------------------------: |
| action         |   ✔️    |                                                                         |
| camera         |   ✔️    |                                                                         |
| collection     |   ✔️    |                                                                         |
| gpencil        |   ✔️    |                                                                         |
| gpencil3        |   ✔️    |                                                                         |
| image          |   ✔️    |                                                                         |
| mesh           |   ✔️    |                                                                         |
| material       |   ✔️    |                                                                         |
| node_groups    |   ✔️    |                        Material & Geometry only                         |
| geometry nodes |   ✔️    |                                                                         |
| metaball       |   ✔️    |                                                                         |
| object         |   ✔️    |                                                                         |
| texts          |   ✔️    |                                                                         |
| scene          |   ✔️    |                                                                         |
| world          |   ✔️    |                                                                         |
| volumes        |   ✔️    |                                                                         |
| lightprobes    |   ✔️    |                                                                         |
| physics        |   ✔️    |                                                                         |
| textures       |   ✔️    |                                                                         |
| curve          |   ❗    |                      Nurbs surfaces not supported                       |
| armature       |   ❗    |          Only for Mesh. [Planned for GPencil](https://gitlab.com/slumber/multi-user/-/issues/161). Not stable yet           |
| particles      |   ❗    |                        The cache isn't syncing.                         |
| speakers       |   ❗    |      [Partial](https://gitlab.com/slumber/multi-user/-/issues/65)       |
| vse            |   ❗    |                     Mask and Clip not supported yet                     |
| libraries      |   ❌    |                                                                         |
| nla            |   ❌    |                                                                         |
| compositing    |   ❌    | [Planned for v0.7.0](https://gitlab.com/slumber/multi-user/-/issues/46) |



### Performance issues

Since this addon is written in pure python for a research purpose, performances could be better from all perspective.
I'm working on it.

## Dependencies

| Dependencies | Version | Needed |
| ------------ | :-----: | -----: |
| Replication  | latest  |    yes |



## Contributing

See [contributing section](https://slumber.gitlab.io/multi-user/ways_to_contribute.html) of the documentation.

Feel free to [join the discord server](https://discord.gg/aBPvGws) to chat, seek help and contribute.

## Licensing

This project is based on [Multi-User](https://gitlab.com/slumber/multi-user) and remains under the [GNU General Public License v3](LICENSE).

See [license](LICENSE)

