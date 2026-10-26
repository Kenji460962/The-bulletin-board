/*
 * talk-ch マスコットの表情を切り替える小さなヘルパー。
 *
 *   var face = TalkChFace.attach(modelViewerElement);   // model-viewer 要素に取り付ける
 *   face.set('happy');                                  // 表情を変える(なめらかに切り替わる)
 *   face.get();                                         // 今の表情
 *
 * 表情一覧: neutral, smile, happy, laugh, wink, doubt, shy, sad, angry, surprised, sleepy, thinking
 *           (まばたきは自動。その表情のまま目だけ閉じる blink_表情名 を使う)
 *
 * 表情はGLBに入っているアニメーション(各表情=1つのポーズ)で、
 * model-viewer のクロスフェード機能で次の表情へなめらかにつなぎます。
 */
(function (w) {
    'use strict';
    var EXPRESSIONS = ['neutral', 'smile', 'happy', 'laugh', 'wink', 'doubt', 'shy', 'sad', 'angry', 'surprised', 'sleepy', 'thinking'];
    var reduce = w.matchMedia && w.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function attach(mv, opts) {
        opts = opts || {};
        var current = opts.initial || 'neutral';
        var autoBlink = opts.blink !== false && !reduce;
        var timer = null, blinkTimer = null, destroyed = false;

        mv.setAttribute('autoplay', '');
        mv.setAttribute('animation-crossfade-duration', String(opts.fadeMs || 160));

        function available(name) {
            var list = mv.availableAnimations || [];
            return list.indexOf(name) >= 0;
        }
        function apply(name) {
            if (destroyed || !available(name)) return false;
            mv.animationName = name;
            try { mv.play(); } catch (e) {}
            return true;
        }
        function scheduleBlink() {
            clearTimeout(timer);
            if (!autoBlink || destroyed) return;
            timer = setTimeout(function () {
                var b = 'blink_' + current;          // 目を閉じられる表情だけまばたきする
                if (available(b)) {
                    apply(b);
                    blinkTimer = setTimeout(function () { apply(current); }, 150);
                }
                scheduleBlink();
            }, 2500 + Math.random() * 3500);
        }
        function set(name) {
            if (EXPRESSIONS.indexOf(name) < 0) { console.warn('[TalkChFace] 未知の表情:', name); return; }
            current = name;
            clearTimeout(blinkTimer);
            apply(name);
        }
        function init() { apply(current); scheduleBlink(); }

        if (mv.loaded) init(); else mv.addEventListener('load', init, { once: true });

        return {
            set: set,
            get: function () { return current; },
            list: function () { return EXPRESSIONS.slice(); },
            destroy: function () { destroyed = true; clearTimeout(timer); clearTimeout(blinkTimer); }
        };
    }

    w.TalkChFace = { attach: attach, expressions: EXPRESSIONS.slice() };
})(window);
