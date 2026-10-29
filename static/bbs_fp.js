/* 端末フィンガープリント: cookieを消されても同じ端末を見分けるための補助識別子。
   結果は bbs_fp cookie に入り、以降の全リクエストに自動で付く(フォームやfetchの改修は不要)。
   サーバー側では「珍しい値のときだけ」BANに使う(app.py の _ban_fp_if_rare)。 */
(function () {
  'use strict';
  function h128(str) {
    // cyrb53 を2系統回して 32桁hex にする(同期・外部依存なし)
    function one(seed) {
      var h1 = 0xdeadbeef ^ seed, h2 = 0x41c6ce57 ^ seed;
      for (var i = 0, ch; i < str.length; i++) {
        ch = str.charCodeAt(i);
        h1 = Math.imul(h1 ^ ch, 2654435761);
        h2 = Math.imul(h2 ^ ch, 1597334677);
      }
      h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
      h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
      return (h2 >>> 0).toString(16).padStart(8, '0') + (h1 >>> 0).toString(16).padStart(8, '0');
    }
    return one(1) + one(2);
  }
  function safe(fn, d) { try { return fn(); } catch (e) { return d; } }

  function canvasSig() {
    var c = document.createElement('canvas'); c.width = 240; c.height = 60;
    var x = c.getContext('2d');
    x.textBaseline = 'top'; x.font = "16px 'Arial'"; x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
    x.fillStyle = '#069'; x.fillText('talk-ch \u3042\u30a2\u4e9c fp \u2603', 2, 15);
    x.fillStyle = 'rgba(102,204,0,0.7)'; x.font = "18px 'Times New Roman'"; x.fillText('talk-ch \u3042\u30a2\u4e9c fp \u2603', 4, 30);
    return c.toDataURL();
  }
  function webglSig() {
    var c = document.createElement('canvas');
    var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return 'nogl';
    var ext = gl.getExtension('WEBGL_debug_renderer_info');
    return ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) + '/' + gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
               : gl.getParameter(gl.VENDOR) + '/' + gl.getParameter(gl.RENDERER);
  }
  function fontSig() {
    var base = ['monospace', 'sans-serif', 'serif'];
    var fonts = ['Arial', 'Courier New', 'Georgia', 'Impact', 'Meiryo', 'MS Gothic', 'Hiragino Sans',
                 'Yu Gothic', 'Noto Sans CJK JP', 'Roboto', 'Segoe UI', 'Helvetica Neue', 'Menlo', 'Ubuntu'];
    var s = document.createElement('span');
    s.style.cssText = 'position:absolute;left:-9999px;font-size:72px;visibility:hidden';
    s.textContent = 'mmmmmmmmmmlli\u3042\u30a2\u4e9c';
    document.documentElement.appendChild(s);
    var bw = base.map(function (b) { s.style.fontFamily = b; return s.offsetWidth; });
    var out = fonts.map(function (f) {
      return base.some(function (b, i) { s.style.fontFamily = "'" + f + "'," + b; return s.offsetWidth !== bw[i]; }) ? 1 : 0;
    }).join('');
    s.remove();
    return out;
  }

  var n = navigator, sc = screen;
  var parts = [
    safe(canvasSig, 'nc'), safe(webglSig, 'nw'), safe(fontSig, 'nf'),
    sc.width + 'x' + sc.height + 'x' + sc.colorDepth, window.devicePixelRatio || 1,
    safe(function () { return Intl.DateTimeFormat().resolvedOptions().timeZone; }, ''),
    (n.languages || [n.language]).join(','), n.platform, n.hardwareConcurrency || 0,
    n.deviceMemory || 0, n.maxTouchPoints || 0
  ];
  var fp = h128(parts.join('||'));
  var secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = 'bbs_fp=' + fp + '; Max-Age=' + (60 * 60 * 24 * 365 * 10) + '; Path=/; SameSite=Lax' + secure;
})();
