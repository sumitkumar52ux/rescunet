/* Shared early-warning module (loaded by index.html and admin.html).
   Exposes: showFlood(data), arm().  The server decides WHO may trigger the siren;
   this file only reacts to the `flood_emergency_alert` event. */
(function () {
  const SIREN_URL = 'https://actions.google.com/sounds/v1/alarms/alarm_clock.ogg';   // royalty-free alarm
  const siren = new Audio(SIREN_URL); siren.loop = true; siren.volume = 1; siren.preload = 'auto';
  let ctx = null, osc = null, sweep = null, vib = null;

  const css = document.createElement('style');
  css.textContent = `@keyframes flashBorder{0%,100%{box-shadow:inset 0 0 0 14px #E53E3E,inset 0 0 90px 20px rgba(229,62,62,.6)}50%{box-shadow:inset 0 0 0 14px #5c1414,inset 0 0 20px 0 rgba(229,62,62,.1)}}
  #floodModal{animation:flashBorder .8s infinite}@media(prefers-reduced-motion:reduce){#floodModal{animation-duration:2.4s}}`;
  document.head.appendChild(css);

  let modal;
  function build() {
    modal = document.createElement('div');
    modal.id = 'floodModal'; modal.setAttribute('role', 'alertdialog'); modal.setAttribute('aria-live', 'assertive');
    modal.style.cssText = 'display:none;position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.92);align-items:center;justify-content:center;padding:24px;text-align:center;color:#fff;font-family:system-ui,sans-serif';
    modal.innerHTML = `<div style="max-width:640px"><div style="font-size:72px">🌊🚨</div>
      <h2 id="floodTitle" style="font-size:44px;font-weight:800;color:#E53E3E;margin:12px 0">FLASH FLOOD WARNING</h2>
      <p id="floodMsg" style="font-size:24px;font-weight:600;margin-bottom:12px"></p>
      <p id="floodMeta" style="color:#cbd5e1;margin-bottom:32px"></p>
      <button id="muteBtn" style="background:#38A169;color:#fff;font-size:24px;font-weight:800;padding:20px 32px;border-radius:12px;border:0;cursor:pointer">⚠️ MUTE SIREN &amp; EVACUATE</button></div>`;
    document.body.appendChild(modal);
    document.getElementById('muteBtn').onclick = mute;
  }
  if (document.body) build(); else document.addEventListener('DOMContentLoaded', build);

  function fallbackOn() {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      osc = ctx.createOscillator(); const g = ctx.createGain(); g.gain.value = .4;
      osc.type = 'sawtooth'; osc.connect(g); g.connect(ctx.destination); osc.start();
      let up = true;
      sweep = setInterval(() => { osc.frequency.linearRampToValueAtTime(up ? 1200 : 600, ctx.currentTime + .9); up = !up; }, 900);
    } catch (e) {}
  }
  function fallbackOff() { clearInterval(sweep); if (osc) { try { osc.stop(); } catch (e) {} osc = null; } }
  function vibOn() { if (!navigator.vibrate) return; navigator.vibrate([1000, 500, 1000]); vib = setInterval(() => navigator.vibrate([1000, 500, 1000]), 2600); }
  function vibOff() { clearInterval(vib); if (navigator.vibrate) navigator.vibrate(0); }

  function mute() {
    siren.pause(); siren.currentTime = 0; fallbackOff(); vibOff();
    modal.style.display = 'none';
  }
  window.showFlood = function (d) {
    const hi = (function () { try { return localStorage.getItem('rn-lang') === 'hi'; } catch (e) { return false; }   // Sound is unlocked silently on the user's first tap anywhere on the page - no 'Arm siren' button needed.
  document.addEventListener('pointerdown', function () { window.arm(true); }, { once: true });
})();
    document.getElementById('floodTitle').textContent = hi ? 'बाढ़ की चेतावनी' : 'FLASH FLOOD WARNING';
    document.getElementById('muteBtn').textContent = hi ? '⚠️ सायरन बंद करें और निकलें' : '⚠️ MUTE SIREN & EVACUATE';
    document.getElementById('floodMsg').textContent = (hi && d.message_hi) || d.message;
    document.getElementById('floodMeta').textContent = (hi ? 'स्रोत: ' : 'Source: ') + d.source + (hi ? ' · अनुमानित समय ' : ' · Estimated lead time ') + d.lead_time + ' · ' + new Date(d.timestamp).toLocaleTimeString();
    modal.style.display = 'flex';
    siren.currentTime = 0; siren.play().catch(fallbackOn); vibOn();
  };
  window.arm = function (quiet) {   // browsers need one user gesture before sound can play
    siren.muted = true;
    siren.play().then(() => { siren.pause(); siren.currentTime = 0; siren.muted = false; }).catch(() => { siren.muted = false; });
    if (quiet === true) return;
    if (navigator.vibrate) navigator.vibrate(60);
    ['armBtn', 'armBtn2'].forEach(id => { const b = document.getElementById(id); if (b) b.textContent = '✓ Siren armed'; });
  };
})();
