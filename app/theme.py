"""見た目の改良（追加のみ）。既存の CSS（web.CSS）の後ろに重ねる。既存のクラスの意味は変えず、色・角丸・影・飾りだけを足す。

- 明るい青をベースに、Instagram / Slack のような、やわらかい面と角丸。ダークモードでも読める配色。
- 飾り（フォース・ライトニング風の細い電流と、魔法の粉のような光の粒）は、ヘッダー付近だけ。低い不透明度で、操作・文字の邪魔をしない。
  `prefers-reduced-motion` では動きを止める。装飾は `aria-hidden`・`pointer-events:none`。
- 外部のファイル・画像は使わない（CSP が、自分のサイトと inline のスタイルだけを許すため）。SVG は、ページに直接埋め込む。
"""

THEME = """
:root{--blue:#2f80ff;--blue2:#3ec5ff;--ink:#14213d;--bg1:#eef5ff;--bg2:#ffffff;--card:#ffffff;--line:#d5e4ff;--shadow:0 6px 24px rgba(47,128,255,.12)}
@media (prefers-color-scheme:dark){:root{--ink:#e9f1ff;--bg1:#0d1b33;--bg2:#0a1326;--card:#13233f;--line:#25406b;--shadow:0 6px 24px rgba(0,0,0,.35)}}
body{color:var(--ink);background:linear-gradient(180deg,var(--bg1),var(--bg2) 320px) no-repeat,var(--bg2);position:relative;min-height:100vh}
h1{font-size:1.5rem;background:linear-gradient(90deg,var(--blue),var(--blue2));-webkit-background-clip:text;background-clip:text;color:transparent;
   position:relative;z-index:1;padding-bottom:.15em}
@media (forced-colors:active){h1{color:CanvasText;background:none}}
a{color:var(--blue)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:14px 16px}
.ai{border-left:5px solid var(--blue)}
input,textarea,select{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
input:focus,textarea:focus,select:focus{outline:2px solid var(--blue2);outline-offset:1px}
button{background:linear-gradient(135deg,var(--blue),var(--blue2));border-radius:999px;font-weight:700;box-shadow:0 3px 10px rgba(47,128,255,.28);
   transition:transform .12s ease,box-shadow .12s ease}
button:hover{transform:translateY(-1px);box-shadow:0 6px 16px rgba(47,128,255,.35)}
button:active{transform:none}
button:focus-visible{outline:3px solid var(--blue2);outline-offset:2px}
.muted{color:#5b6f94}@media (prefers-color-scheme:dark){.muted{color:#9db4dc}}
/* 装飾: ヘッダー付近の細い電流と、光の粒 */
.fx{position:absolute;left:0;right:0;top:0;height:120px;overflow:hidden;pointer-events:none;z-index:0}
.fx svg{position:absolute;right:-6px;top:-6px;width:210px;height:120px;opacity:.16}
.fx path{fill:none;stroke:var(--blue2);stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 3px var(--blue2))}
.fx .b2{opacity:.7}
.fx i{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--blue2);box-shadow:0 0 6px 1px var(--blue2);opacity:.0}
.fx i:nth-child(2){left:62%;top:26px}.fx i:nth-child(3){left:78%;top:58px}.fx i:nth-child(4){left:90%;top:20px}.fx i:nth-child(5){left:70%;top:84px}
@keyframes flick{0%,86%,100%{opacity:.16}88%{opacity:.34}90%{opacity:.08}93%{opacity:.3}}
@keyframes dust{0%,100%{opacity:0;transform:translateY(0)}50%{opacity:.5;transform:translateY(-6px)}}
.fx svg{animation:flick 6s infinite}
.fx i{animation:dust 4.5s ease-in-out infinite}.fx i:nth-child(3){animation-delay:1.2s}.fx i:nth-child(4){animation-delay:2.4s}.fx i:nth-child(5){animation-delay:3.3s}
/* AI が考えている間だけ、縁がゆっくり光る */
@keyframes glow{0%,100%{box-shadow:0 0 0 0 rgba(62,197,255,.0)}50%{box-shadow:0 0 14px 2px rgba(62,197,255,.35)}}
.thinking{animation:glow 2.4s ease-in-out infinite}
/* 統合分析の結果: 1 画面のカード列 */
.flow{display:grid;gap:10px;margin:.8em 0}
.flow .step{display:flex;gap:10px;align-items:flex-start}
.flow .n{flex:0 0 28px;height:28px;border-radius:50%;background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;font-weight:700;display:grid;place-items:center;font-size:.9rem}
.flow .step>div{flex:1;min-width:0}
.flow table{border-collapse:collapse;width:100%;font-size:.92rem}
.flow th{background:var(--blue);color:#fff;text-align:left;padding:.35em .5em}
.flow td{border-bottom:1px solid var(--line);padding:.35em .5em}
.flow .tblwrap{overflow-x:auto}
.choices{display:flex;flex-wrap:wrap;gap:8px;margin:.5em 0}
.choices form{margin:0}.choices button,.choices a.btn{width:auto;margin:0;padding:.55em 1.1em}
a.btn{display:inline-block;background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;border-radius:999px;font-weight:700;text-decoration:none;box-shadow:0 3px 10px rgba(47,128,255,.28)}
a.btn.sub,button.sub{background:transparent;color:var(--blue);border:1px solid var(--blue);box-shadow:none}
@media (prefers-reduced-motion:reduce){.fx svg,.fx i,.thinking{animation:none}.fx i{opacity:.3}.fx svg{opacity:.14}button{transition:none}button:hover{transform:none}}
"""

# 装飾（ページの先頭に 1 つ）。稲妻は 2 本の細い線。意味はなく、スクリーンリーダーには読ませない
FX = ('<div class="fx" aria-hidden="true"><svg viewBox="0 0 210 120" focusable="false">'
      '<path d="M205 4 L168 44 L184 46 L140 96 L156 98 L118 118"/>'
      '<path class="b2" d="M170 4 L150 30 L160 32 L132 62"/></svg><i></i><i></i><i></i><i></i></div>')
