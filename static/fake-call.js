/* EvidenceTap Exit Call: local UI simulation only; no telecom call or recording. */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const overlay=byId("fake-call-overlay"), incoming=byId("incoming-call"), active=byId("active-fake-call");
  if (!overlay || !incoming || !active) return;
  let startedAt=0, timerId=null, previousFocus=null;
  function showOverlay(){ previousFocus=document.activeElement; incoming.classList.remove("hide"); active.classList.add("hide"); overlay.classList.remove("hide"); document.body.style.overflow="hidden"; byId("accept-fake-call").focus(); }
  function closeOverlay(){ if(timerId!==null) window.clearInterval(timerId); timerId=null; overlay.classList.add("hide"); document.body.style.overflow=""; byId("fake-call-mute").setAttribute("aria-pressed","false"); byId("fake-call-speaker").setAttribute("aria-pressed","false"); if(previousFocus && previousFocus.focus) previousFocus.focus(); }
  function updateDuration(){ const s=Math.floor((Date.now()-startedAt)/1000); byId("fake-call-duration").textContent=String(Math.floor(s/60)).padStart(2,"0")+":"+String(s%60).padStart(2,"0"); }
  byId("start-fake-call").addEventListener("click",showOverlay);
  byId("decline-fake-call").addEventListener("click",closeOverlay); byId("end-fake-call").addEventListener("click",closeOverlay);
  byId("accept-fake-call").addEventListener("click",()=>{ incoming.classList.add("hide"); active.classList.remove("hide"); startedAt=Date.now(); updateDuration(); timerId=window.setInterval(updateDuration,1000); byId("end-fake-call").focus(); });
  ["fake-call-mute","fake-call-speaker"].forEach(id=>byId(id).addEventListener("click",()=>{ const b=byId(id); b.setAttribute("aria-pressed",String(b.getAttribute("aria-pressed")!=="true")); }));
  overlay.addEventListener("click",e=>{ if(e.target===overlay) closeOverlay(); });
  document.addEventListener("keydown",e=>{ if(e.key==="Escape" && !overlay.classList.contains("hide")) closeOverlay(); });
})();
