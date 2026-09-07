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

## JDCOS recovery image

The optional Telnet-recovery workflow is locked to the unmodified
`JDCOS-JDC02-4.3.0.r4211-9e319914fce041a0519e4445c4b77372-single-signed.img`
image. JDCOS and the firmware image are products of JDCloud/Jingdong and are not
licensed as part of this project's source code. Their names and checksums are
included only so the application can reject a changed or incorrect image.
