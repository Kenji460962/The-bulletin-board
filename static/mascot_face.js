/*
 * talk-ch マスコットの表情を切り替える小さなヘルパー。
 *
 *   var face = TalkChFace.attach(modelViewerElement);   // model-viewer 要素に取り付ける
 *   face.set('happy');                                  // 基本の表情を変える(なめらかに切り替わる)
 *   face.get();                                         // 今の基本の表情
 *
 * 表情一覧: neutral, smile, happy, laugh, wink, doubt, shy, sad, angry, surprised, sleepy, thinking
 *
 * 取り付けただけで、次の「生きている感じ」の動きが自動で入ります。
 *   - まばたき: 4〜9秒おきと控えめ(まれに二度まばたき)。その表情のまま目だけ閉じる(blink_表情名)
 *     小さな動きなので、OSの「視覚効果を減らす」がオンでも行う
 *   - 表情のゆらぎ: 4〜9秒おきに、基本の表情から少しだけ別の表情へ(1〜3秒)→ もとに戻る
 *     (例: ふつう → にっこり / 考え中 → ふつう)。強い表情(怒り・大笑いなど)は揺らさない
 *     「視覚効果を減らす」がオンのときは、このゆらぎだけ止める
 *   オプション: attach(mv, { blink: false, idle: false }) でそれぞれ止められる
 *
 * 表情はGLBに入っているアニメーション(各表情=1つのポーズ)で、
 * model-viewer のクロスフェード機能で次の表情へなめらかにつなぎます。
 */
(function (w) {
    'use strict';
    var EXPRESSIONS = ['neutral', 'smile', 'happy', 'laugh', 'wink', 'doubt', 'shy', 'sad', 'angry', 'surprised', 'sleepy', 'thinking'];
    // 基本の表情ごとの「ちょっとした揺らぎ」。ここに無い表情は揺らさない。
    var IDLE_VARIANTS = {
        neutral:   ['smile', 'thinking', 'doubt'],
        smile:     ['neutral', 'happy'],
        happy:     ['smile'],
        doubt:     ['neutral', 'thinking'],
        thinking:  ['neutral', 'doubt'],
        surprised: ['neutral'],
        shy:       ['smile'],
        sleepy:    ['neutral']
    };
    var reduce = w.matchMedia && w.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function rand(a, b) { return a + Math.random() * (b - a); }

    function attach(mv, opts) {
        opts = opts || {};
        var base = opts.initial || 'neutral';   // 呼び出し側が決めた基本の表情
        var shown = base;                        // いま実際に出している表情(揺らぎ中は base と違う)
        var autoBlink = opts.blink !== false;          // まばたきは小さな動きなので常に行う
        var autoIdle = opts.idle !== false && !reduce;
        var blinkTimer = null, blinkEnd = null, idleTimer = null, idleEnd = null, destroyed = false;

        mv.setAttribute('autoplay', '');
        mv.setAttribute('animation-crossfade-duration', String(opts.fadeMs || 160));

        function available(name) { return (mv.availableAnimations || []).indexOf(name) >= 0; }
        function apply(name) {
            if (destroyed || !available(name)) return false;
            mv.animationName = name;
            try { mv.play(); } catch (e) {}
            return true;
        }
        function show(name) { shown = name; apply(name); }

        // ---- まばたき ----
        function blinkOnce(then) {
            var b = 'blink_' + shown;            // 目を閉じられる表情だけまばたきする
            if (!available(b)) { if (then) then(); return; }
            var keep = shown;
            apply(b);
            blinkEnd = setTimeout(function () {
                if (shown === keep) apply(keep);
                if (then) then();
            }, 130);
        }
        function scheduleBlink() {
            clearTimeout(blinkTimer);
            if (!autoBlink || destroyed) return;
            blinkTimer = setTimeout(function () {
                blinkOnce(function () {
                    if (Math.random() < 0.08) {          // まれに二度まばたき
                        blinkEnd = setTimeout(function () { blinkOnce(); }, 180);
                    }
                });
                scheduleBlink();
            }, rand(4000, 9000));
        }

        // ---- 表情のゆらぎ ----
        function scheduleIdle() {
            clearTimeout(idleTimer);
            if (!autoIdle || destroyed) return;
            idleTimer = setTimeout(function () {
                var list = IDLE_VARIANTS[base];
                if (list && shown === base) {
                    var v = list[Math.floor(Math.random() * list.length)];
                    if (available(v)) {
                        show(v);
                        idleEnd = setTimeout(function () { if (!destroyed && shown === v) show(base); }, rand(1000, 3000));
                    }
                }
                scheduleIdle();
            }, rand(4000, 9000));
        }

        function stopSideEffects() { clearTimeout(blinkEnd); clearTimeout(idleEnd); }

        function set(name) {
            if (EXPRESSIONS.indexOf(name) < 0) { console.warn('[TalkChFace] 未知の表情:', name); return; }
            stopSideEffects();
            base = name;
            show(name);
        }
        function init() {
            if (!(mv.availableAnimations || []).length) {
                console.warn('[TalkChFace] このGLBに表情データがありません。古い talk_ch_mascot.glb(約540KB以下)が' +
                             '配信されている可能性があります。新しいもの(約610KB)に差し替えて、キャッシュをパージしてください。');
                return;
            }
            show(base);
            scheduleBlink();
            scheduleIdle();
        }

        if (mv.loaded) init(); else mv.addEventListener('load', init, { once: true });

        return {
            set: set,
            get: function () { return base; },
            list: function () { return EXPRESSIONS.slice(); },
            destroy: function () {
                destroyed = true;
                clearTimeout(blinkTimer); clearTimeout(idleTimer); stopSideEffects();
            }
        };
    }

    w.TalkChFace = { attach: attach, expressions: EXPRESSIONS.slice() };
})(window);
