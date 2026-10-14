/* A normal verified link, never a proxy or a copy of the ticket store. */
(async()=>{
  const link=document.getElementById('support-link');
  link.addEventListener('click',e=>{if(link.getAttribute('aria-disabled')==='true'){e.preventDefault();document.getElementById('notice').textContent='Separate Support is unavailable. Start Support mode using the existing launcher.';}});
  try {const r=await fetch('/api/support');const s=await r.json();if(r.ok&&s.available&&s.url){link.href=s.url;link.setAttribute('aria-disabled','false');link.title='Open the independent Support Desk';}}
  catch {link.title='Separate Support is currently unavailable';}
})();
