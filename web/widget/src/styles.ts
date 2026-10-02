import { tokensCss } from "./tokens.ts";

/** Widget styles. Every colour, radius, space, shadow and duration comes from a token. */
export function widgetCss(): string {
  return `${tokensCss(":host")}
:host{all:initial;position:fixed;right:var(--bap-space-xl);bottom:var(--bap-space-xl);
z-index:2147483000;font-family:var(--bap-font-family);font-size:var(--bap-font-size);
line-height:var(--bap-font-line-height);color:var(--bap-color-ink);
--bap-accent:var(--bap-color-accent);--bap-accent-text:#111111}
*,*::before,*::after{box-sizing:border-box}
button,textarea{font:inherit;color:inherit}
:focus{outline:none}
:focus-visible{box-shadow:var(--bap-shadow-focus)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
clip:rect(0,0,0,0);white-space:nowrap;border:0}
.launcher{width:60px;height:60px;border:0;border-radius:var(--bap-radius-pill);
background:var(--bap-color-ink);color:#fff;display:grid;place-items:center;cursor:pointer;
box-shadow:var(--bap-shadow-launcher);margin-left:auto;
transition:transform var(--bap-motion-fast) var(--bap-motion-easing)}
.launcher:hover{transform:translateY(-2px)}
.launcher svg{width:26px;height:26px}
.panel{position:absolute;right:0;bottom:76px;width:380px;max-width:calc(100vw - 32px);
height:600px;max-height:calc(100vh - 120px);display:flex;flex-direction:column;
background:var(--bap-color-canvas);border-radius:var(--bap-radius-panel);
box-shadow:var(--bap-shadow-panel);overflow:hidden;visibility:hidden;opacity:0;
transform:translateY(12px) scale(.98);transform-origin:bottom right;
transition:opacity var(--bap-motion-base) var(--bap-motion-easing),
transform var(--bap-motion-base) var(--bap-motion-easing),visibility 0s linear var(--bap-motion-base)}
.panel[data-open]{visibility:visible;opacity:1;transform:none;transition-delay:0s}
.header{display:flex;align-items:center;gap:var(--bap-space-md);
padding:var(--bap-space-lg) var(--bap-space-lg) var(--bap-space-lg) var(--bap-space-xl);
background:var(--bap-color-surface);box-shadow:var(--bap-shadow-soft)}
.avatar{width:40px;height:40px;border-radius:var(--bap-radius-pill);background:var(--bap-accent);
color:var(--bap-accent-text);display:grid;place-items:center;font-weight:700}
.title{flex:1;min-width:0}
.name{font-weight:650;font-size:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status{display:flex;align-items:center;gap:6px;font-size:var(--bap-font-label);
text-transform:uppercase;letter-spacing:.08em;color:var(--bap-color-muted)}
.dot{width:8px;height:8px;border-radius:var(--bap-radius-pill);background:var(--bap-color-online)}
.icon-button{width:40px;height:40px;border:0;border-radius:var(--bap-radius-pill);
background:var(--bap-color-subtle);display:grid;place-items:center;cursor:pointer;
transition:background var(--bap-motion-fast) var(--bap-motion-easing)}
.icon-button:hover{background:var(--bap-color-subtle-hover)}
.icon-button svg{width:18px;height:18px}
.messages{flex:1;overflow-y:auto;padding:var(--bap-space-xl);display:flex;flex-direction:column;
gap:var(--bap-space-lg);overscroll-behavior:contain}
.msg{display:flex;flex-direction:column;max-width:86%;animation:bap-in var(--bap-motion-base) var(--bap-motion-easing)}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.assistant,.msg.error{align-self:flex-start;align-items:flex-start}
.bubble{padding:var(--bap-space-md) var(--bap-space-lg);border-radius:var(--bap-radius-bubble);
white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:var(--bap-shadow-soft)}
.user .bubble{background:var(--bap-color-subtle);color:var(--bap-color-ink);border-bottom-right-radius:6px}
.assistant .bubble{background:var(--bap-accent);color:var(--bap-accent-text);border-bottom-left-radius:6px}
.bubble a{color:inherit;text-decoration:underline;text-underline-offset:2px}
.cite{font-size:.72em;font-weight:700;margin-left:1px;opacity:.85}
.meta{font-size:var(--bap-font-small);color:var(--bap-color-muted);margin-top:var(--bap-space-xs);padding:0 6px}
.sources{display:flex;flex-wrap:wrap;gap:6px;margin:var(--bap-space-sm) 0 0;padding:0;list-style:none}
.chip{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:5px 12px 5px 9px;
border-radius:var(--bap-radius-pill);background:var(--bap-color-violet-soft);color:var(--bap-color-violet-ink);
font-size:var(--bap-font-small);font-weight:550}
.chip svg{width:13px;height:13px;flex:none}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.typing{display:inline-flex;gap:5px;padding:var(--bap-space-md) var(--bap-space-lg)}
.typing i{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.45;
animation:bap-dot 1.1s infinite var(--bap-motion-easing)}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}
.error .bubble{background:var(--bap-color-danger-soft);color:var(--bap-color-danger)}
.retry{margin-top:var(--bap-space-sm);border:0;border-radius:var(--bap-radius-pill);
background:var(--bap-color-ink);color:#fff;padding:7px 16px;font-size:13px;font-weight:600;cursor:pointer}
.retry[disabled]{opacity:.55;cursor:not-allowed}
.suggestions{display:flex;flex-wrap:wrap;gap:var(--bap-space-sm);padding:0 var(--bap-space-xl) var(--bap-space-md)}
.suggestion{border:0;border-radius:var(--bap-radius-pill);background:var(--bap-color-surface);
padding:8px 14px;font-size:13.5px;cursor:pointer;box-shadow:var(--bap-shadow-soft);
transition:background var(--bap-motion-fast) var(--bap-motion-easing)}
.suggestion:hover{background:var(--bap-color-subtle)}
.composer{display:flex;align-items:flex-end;gap:var(--bap-space-sm);margin:0 var(--bap-space-lg) var(--bap-space-lg);
padding:6px 6px 6px var(--bap-space-xl);background:var(--bap-color-surface);
border-radius:var(--bap-radius-input);box-shadow:var(--bap-shadow-soft)}
.composer:focus-within{box-shadow:var(--bap-shadow-soft),var(--bap-shadow-focus)}
textarea{flex:1;border:0;resize:none;background:transparent;padding:9px 0;max-height:120px;
line-height:var(--bap-font-line-height)}
textarea::placeholder{color:var(--bap-color-muted)}
textarea:focus-visible{box-shadow:none}
.send{width:42px;height:42px;flex:none;border:0;border-radius:var(--bap-radius-pill);
background:var(--bap-color-ink);color:#fff;display:grid;place-items:center;cursor:pointer;
transition:transform var(--bap-motion-fast) var(--bap-motion-easing)}
.send:hover{transform:scale(1.05)}
.send[disabled]{opacity:.45;cursor:not-allowed;transform:none}
.send svg{width:18px;height:18px}
@keyframes bap-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes bap-dot{0%,80%,100%{transform:translateY(0);opacity:.35}40%{transform:translateY(-4px);opacity:.9}}
@media (max-width:480px){:host{right:16px;bottom:16px}
.panel{position:fixed;inset:0;width:100%;max-width:none;height:100%;max-height:none;border-radius:0}
:host([data-open]) .launcher{visibility:hidden}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
`;
}
