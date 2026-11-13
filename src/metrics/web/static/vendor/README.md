# Vendored JavaScript

Served locally so the page loads nothing from external servers. All BSD-3-Clause, see the `LICENSE-*` files.

| File | Package | Version | Source |
| --- | --- | --- | --- |
| `vega.min.js` | vega | 5.30.0 | https://cdn.jsdelivr.net/npm/vega@5.30.0/build/vega.min.js |
| `vega-lite.min.js` | vega-lite | 5.21.0 | https://cdn.jsdelivr.net/npm/vega-lite@5.21.0/build/vega-lite.min.js |
| `vega-embed.min.js` | vega-embed | 6.26.0 | https://cdn.jsdelivr.net/npm/vega-embed@6.26.0/build/vega-embed.min.js |
| `vega-interpreter.min.js` | vega-interpreter | 1.0.5 | https://cdn.jsdelivr.net/npm/vega-interpreter@1.0.5/build/vega-interpreter.min.js |

`vega-interpreter` evaluates Vega expressions without `eval` or `new Function`, so the pages work under the strict Content-Security-Policy (`app.js` passes it to vega-embed).

`vega-embed.css` holds the styles vega-embed and its tooltip would otherwise inject as `<style>` elements, copied verbatim from `vega-embed.min.js`; update it together with vega-embed.
