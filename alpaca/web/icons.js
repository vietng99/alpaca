// Original geometric interface icons. No external runtime or image requests.
const paths = {
 cockpit:'<path d="M3 17a9 9 0 1 1 18 0M12 13l5-5M5 17h14"/><circle cx="12" cy="13" r="2"/>',
 overview:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
 work:'<rect x="5" y="4" width="15" height="17" rx="2"/><path d="M9 3h7v4H9zM9 12l2 2 5-5M9 18h7"/>',
 runs:'<path d="m8 4 12 8-12 8zM3 4v16"/>',
 live:'<rect x="3" y="4" width="8" height="7" rx="1"/><rect x="13" y="4" width="8" height="7" rx="1"/><rect x="3" y="13" width="18" height="7" rx="1"/><path d="M6 16.5h6"/>',
 activity:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',
 messages:'<path d="M21 14a3 3 0 0 1-3 3H9l-6 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3zM7 8h10M7 12h7"/>',
 library:'<path d="M4 3h5v18H4zM12 3h4v18h-4zM18 4l3 16"/>',
 sessions:'<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4M7 8l3 3-3 3M13 13h4"/>',
 search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
 arrow:'<path d="M4 12h15m-6-6 6 6-6 6"/>',
 chevron:'<path d="m9 5 7 7-7 7"/>',
 external:'<path d="M14 3h7v7M21 3 10 14M10 3H4v17h16v-6"/>',
 refresh:'<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
 close:'<path d="m6 6 12 12M6 18 18 6"/>',
 menu:'<path d="M3 6h18M3 12h18M3 18h18"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 alert:'<path d="m12 3 10 18H2zM12 9v5M12 17h.01"/>',
 file:'<path d="M13 3H5v18h14V9zM13 3v6h6M8 13h8M8 17h6"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
 code:'<path d="m8 5-6 7 6 7M16 5l6 7-6 7M14 3l-4 18"/>',
 pause:'<path d="M8 4v16M16 4v16"/>',
 download:'<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>'
};
export const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.file}</svg>`;
export function hydrate(root=document){root.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);el.removeAttribute('data-icon');});}
