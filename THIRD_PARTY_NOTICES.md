# Third-party notices

This repository contains custom AstrBot plugins and deployment integration files. AstrBot and SnowLuma are external runtime dependencies, not vendored application frameworks.

`deploy/novnc-ui-fixed.js` is a modified noVNC UI module. Its upstream copyright header is retained. It remains under Mozilla Public License 2.0; the license and upstream notice are included in `licenses/`. The compatibility modifications handle credential submission after a disconnected RFB session and invalid resize settings.

`deploy/snow-start-fixed.sh` derives from the SnowLuma image startup script. It includes file-descriptor-limit and desktop-startup compatibility adjustments. See `licenses/SnowLuma-LICENSE` for the upstream license. These files are source copies from the deployed image, with account-specific identifiers removed where applicable.

The daily Qzone adapter also lists implementation references in `plugins/astrbot_plugin_jinli_qzone/README.md`.
