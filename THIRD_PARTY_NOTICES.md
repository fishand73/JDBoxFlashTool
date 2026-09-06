# Third-party notices

## uBootEnter

The network U-Boot interruption protocol and physical-interface filtering in
`src/jdbox_athena/uboot_enter.py` are adapted from
[chenxin527/uBootEnter](https://github.com/chenxin527/uBootEnter), commit
`1c4185c49653465bbfa437c7fdf45c5eabcc7039` (2026-07-31).

Copyright (c) 2026 chenxin527. Licensed under the MIT License. A copy of the
upstream license is included at `third_party/uBootEnter/LICENSE`.

The integration keeps the upstream packet values and behavior, while replacing
Requests and Colorama with the Python standard library and the project's logger.
Scapy remains an optional dependency; Npcap is required on Windows.
