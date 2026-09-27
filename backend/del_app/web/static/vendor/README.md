Vendored AG Grid Community 32.3.3 (MIT). Loaded from `/static/vendor/` so the UI
stays CSP `script-src 'self'` with no CDN. Source: https://github.com/ag-grid/ag-grid

`ag-theme-quartz.css` is patched: its icon font was an inline `data:` URL, which
the nginx CSP (`font-src 'self'`) blocks (icons render as empty boxes). The font
is extracted to `ag-grid-quartz-icons.woff2` and referenced by path. Redo this
if the vendored CSS is ever upgraded.
