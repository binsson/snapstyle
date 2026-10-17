/* AI Designer - Pinterest-style flow
 *
 * Single product image upload (top-level, persisted only in memory) →
 * pick a style card → generation runs in the background. Each generation
 * appears in the "我的" view as a card (loading → done/error).
 *
 * Layout: bottom dock (home/upload/my) + top category tabs + responsive
 * auto-fill card grid.
 */
(function () {
  // ---- State ----
  let refImage = null;                                                  // base64 data URL
  let authToken = localStorage.getItem('aid_token') || '';
  let accountsMode = false;   // server has email accounts + credits (DATABASE_URL set)
  let registrationEnabled = false;
  let authMode = 'login';     // 'login' | 'register' — only used in accounts mode
  let turnstileSiteKey = '';
  let turnstileWidgetId = null;
  let turnstileLoadPromise = null;
  let currentCategoryId = null;
  let searchQuery = '';                                                  // active header search, filters the gallery in place
  let currentView = 'home';                                              // 'home' | 'my'
  // 即刻出图 mode is temporarily disabled — always start in gallery mode.
  // (Previously read from localStorage; intentionally ignored for now.)
  let currentTopMode = 'gallery';                                        // 'gallery' | 'quick'
  let currentMyTab = localStorage.getItem('aid_my_tab_v1') || 'gallery'; // 'favorites' | 'gallery'
  let quickCount = parseInt(localStorage.getItem('aid_quick_count_v1') || '3', 10); // 1-6
  // Output size is fixed to portrait (the header ratio switcher was removed).
  // RATIO_SIZES still validates sizes stored on old items so regeneration
  // keeps their original square/landscape shape.
  const RATIO_SIZES = new Set(['1024x1536', '1024x1024', '1536x1024']);
  const currentRatio = '1024x1536';
  let myItems = [];                                                      // populated in init
  let dynamicStyleAssets = [];
  let favoriteTemplates = [];
  let favoriteCounts = {};  // { template_id: number } — server-backed counts

  const $ = s => document.querySelector(s);
  const LS_MY = 'aid_my_items_v1';
  const LS_FAVORITES = 'aid_favorite_templates_v1';
  const DB_NAME = 'aid';
  const DB_STORE = 'my_items';
  const BLOCKED_STYLE_IMAGES = new Set([
    '/reference_uploads/templates/apparel/20260527074638-214-14.jpg',
    '/reference_uploads/templates/apparel/20260527074637-213-14.jpg',
    '/reference_uploads/templates/apparel/20260527075429-239-14.jpg',
    '/reference_uploads/templates/apparel/20260527075430-240-14.jpg',
    '/reference_uploads/templates/apparel/20260527075432-241-14.jpg',
    '/reference_uploads/templates/apparel/20260527075433-242-14.jpg',
    '/reference_uploads/templates/apparel/20260527075435-244-14.jpg',
    '/reference_uploads/templates/apparel/20260527075436-245-14.jpg',
    '/reference_uploads/templates/apparel/20260527075454-265-14.jpg',
    '/reference_uploads/templates/apparel/20260527075457-268-14.jpg',
    '/reference_uploads/templates/apparel/20260527075458-269-14.jpg',
  ]);
  function isBlockedStyleImage(image) {
    return !!image && BLOCKED_STYLE_IMAGES.has(image);
  }

  // ---- IndexedDB (replaces localStorage; tens of MB, no quota trimming) ----
  let _dbPromise = null;
  function openDB() {
    if (_dbPromise) return _dbPromise;
    _dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onerror = () => reject(req.error);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
          db.createObjectStore(DB_STORE, { keyPath: 'id' });
        }
      };
      req.onsuccess = () => resolve(req.result);
    });
    return _dbPromise;
  }
  function dbPutAll(items) {
    return openDB().then(db => new Promise((resolve, reject) => {
      const tx = db.transaction(DB_STORE, 'readwrite');
      const store = tx.objectStore(DB_STORE);
      store.clear();
      items.forEach(it => store.put(it));
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    }));
  }
  function dbPut(item) {
    return openDB().then(db => new Promise((resolve, reject) => {
      const tx = db.transaction(DB_STORE, 'readwrite');
      tx.objectStore(DB_STORE).put(item);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    }));
  }
  function dbDelete(id) {
    return openDB().then(db => new Promise((resolve, reject) => {
      const tx = db.transaction(DB_STORE, 'readwrite');
      tx.objectStore(DB_STORE).delete(id);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    }));
  }
  // Incremental single-item save. The old approach (clear + rewrite the whole
  // store on every change) rewrote hundreds of MB per generation once the
  // gallery grew, freezing the main thread.
  let _quotaWarned = false;
  function saveItem(item) {
    dbPut(item).catch(err => {
      console.warn('[aid] db save failed:', err);
      if (err && err.name === 'QuotaExceededError' && !_quotaWarned) {
        _quotaWarned = true;
        toast('本地存储已满，请下载并删除一些旧作品', 'error');
      }
    });
  }
  // Cap the gallery so storage can't grow without bound (each item carries a
  // multi-MB product photo + result). Oldest finished items go first; in-flight
  // ones are never trimmed.
  const MAX_MY_ITEMS = 120;
  function trimMyItems() {
    for (let i = myItems.length - 1; i >= 0 && myItems.length > MAX_MY_ITEMS; i--) {
      if (myItems[i].status !== 'loading') {
        const removed = myItems.splice(i, 1)[0];
        dbDelete(removed.id).catch(() => {});
      }
    }
  }
  function dbGetAll() {
    return openDB().then(db => new Promise((resolve, reject) => {
      const tx = db.transaction(DB_STORE, 'readonly');
      const req = tx.objectStore(DB_STORE).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    }));
  }

  async function loadMyItems() {
    try {
      let items = await dbGetAll();
      // One-time migration from the legacy localStorage bucket
      const lsRaw = localStorage.getItem(LS_MY);
      if (lsRaw && items.length === 0) {
        try {
          const lsItems = JSON.parse(lsRaw);
          if (Array.isArray(lsItems) && lsItems.length) {
            await dbPutAll(lsItems);
            items = lsItems;
            console.info('[aid] migrated', lsItems.length, 'items from localStorage to IndexedDB');
          }
        } catch (e) {}
      }
      if (lsRaw) { try { localStorage.removeItem(LS_MY); } catch (e) {} }
      items.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
      return items.map(it => {
        let fixed = it;
        if (it.status === 'loading') {
          // Jobs run server-side now, so an in-flight item survives a reload:
          // keep it loading and resume polling, unless it's old enough that
          // the server-side job must be long gone.
          const recoverable = it.jobId && (Date.now() - (it.createdAt || 0) < JOB_TIMEOUT_MS);
          if (!recoverable) fixed = { ...it, status: 'error', errMsg: '生成中断，点「换一张」即可重新生成' };
        }
        return fixed.seen === undefined ? { ...fixed, seen: true } : fixed;
      });
    } catch (e) {
      console.warn('[aid] db load failed, falling back to empty:', e);
      return [];
    }
  }

  function loadFavoriteTemplates() {
    try {
      const raw = localStorage.getItem(LS_FAVORITES);
      const items = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(items)) return [];
      const cleaned = items.filter(t => t && t.id && !isBlockedStyleImage(t.sampleImage));
      // Persist the cleanup so blocked favorites disappear permanently, not
      // just get hidden on render. Saves storage and survives across reloads
      // even if the block list is later removed.
      if (cleaned.length !== items.length) {
        localStorage.setItem(LS_FAVORITES, JSON.stringify(cleaned));
      }
      return cleaned;
    } catch (e) {
      return [];
    }
  }
  function saveFavoriteTemplates() {
    try {
      localStorage.setItem(LS_FAVORITES, JSON.stringify(favoriteTemplates));
    } catch (e) {
      console.warn('[aid] favorite save failed:', e);
    }
  }
  // ---- Helpers ----
  const ICON_HEART = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.8 4.6a5.4 5.4 0 00-7.6 0L12 5.8l-1.2-1.2a5.4 5.4 0 00-7.6 7.6L12 21l8.8-8.8a5.4 5.4 0 000-7.6z"/></svg>';
  function hide(el) { if (el) el.classList.add('hidden'); }
  function show(el) { if (el) el.classList.remove('hidden'); }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  function toast(msg, type = '') {
    let w = $('.toast-wrap');
    if (!w) { w = document.createElement('div'); w.className = 'toast-wrap'; document.body.appendChild(w); }
    const t = document.createElement('div');
    t.className = 'toast ' + (type || '');
    t.textContent = msg;
    w.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 0.3s'; setTimeout(() => t.remove(), 300); }, 3000);
  }
  function uniqueId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }
  // Loading stage hints, keyed by elapsed seconds. Real generations take
  // 1-3+ minutes (LLM brief + image model + retries), so the thresholds are
  // spread over that range — "即将完成" must only appear near the realistic
  // tail, or users read it as a hang and burn quota on retries.
  const LOADING_STAGES = [
    [0,   'AI 正在分析产品…'],
    [12,  '正在设计拍摄方案…'],
    [35,  '模特试装中…'],
    [60,  '布景打光中…'],
    [95,  '正在拍摄成片…'],
    [140, '精修画面细节…'],
    [180, '正在校准色彩…'],
    [240, '即将完成…'],
  ];
  // ---- Onboarding tour (first visit only) ----
  const TOUR_STEPS = [
    { target: '#dock-upload', title: '上传产品图', desc: '点击这里上传你的产品照片，支持 JPG / PNG 格式。' },
    { target: '#template-grid .template-card', title: '选择灵感图', desc: '看到喜欢的风格图，点一下即可生成同审美的产品图。' },
    { target: '#dock-my', title: '查看作品', desc: '生成完的作品会出现在这里，可以下载或换一张。' },
  ];
  let _tourOverlay, _tourSpotlight, _tourTooltip, _tourStep = 0;

  function startTour() {
    if (localStorage.getItem('aid_tour_done')) return;
    // Create overlay
    _tourOverlay = document.createElement('div');
    _tourOverlay.className = 'tour-overlay';
    _tourOverlay.addEventListener('click', (e) => {
      if (e.target === _tourOverlay) advanceTour();
    });
    _tourSpotlight = document.createElement('div');
    _tourSpotlight.className = 'tour-spotlight';
    _tourTooltip = document.createElement('div');
    _tourTooltip.className = 'tour-tooltip';
    document.body.appendChild(_tourOverlay);
    document.body.appendChild(_tourSpotlight);
    document.body.appendChild(_tourTooltip);
    _tourStep = 0;
    showTourStep();
  }

  function showTourStep() {
    const step = TOUR_STEPS[_tourStep];
    if (!step) { endTour(); return; }
    const el = document.querySelector(step.target);
    if (!el) { _tourStep++; showTourStep(); return; }

    // Position spotlight
    const rect = el.getBoundingClientRect();
    const pad = 8;
    _tourSpotlight.style.left = (rect.left - pad) + 'px';
    _tourSpotlight.style.top = (rect.top - pad) + 'px';
    _tourSpotlight.style.width = (rect.width + pad * 2) + 'px';
    _tourSpotlight.style.height = (rect.height + pad * 2) + 'px';

    // Build tooltip content
    const total = TOUR_STEPS.length;
    const dots = TOUR_STEPS.map((_, i) =>
      `<span class="tour-dot${i === _tourStep ? ' active' : ''}"></span>`
    ).join('');
    const isLast = _tourStep === total - 1;
    _tourTooltip.innerHTML = `
      <div class="tour-tooltip-step">步骤 ${_tourStep + 1} / ${total}</div>
      <div class="tour-tooltip-title">${step.title}</div>
      <div class="tour-tooltip-desc">${step.desc}</div>
      <div class="tour-tooltip-footer">
        <div class="tour-dots">${dots}</div>
        <div style="display:flex;gap:8px;align-items:center">
          ${!isLast ? '<button class="tour-skip" id="tour-skip">跳过</button>' : ''}
          <button class="tour-next" id="tour-next">${isLast ? '开始使用' : '下一步'}</button>
        </div>
      </div>
    `;
    document.getElementById('tour-next')?.addEventListener('click', advanceTour);
    document.getElementById('tour-skip')?.addEventListener('click', endTour);

    // Position tooltip
    positionTooltip(rect);
  }

  function positionTooltip(targetRect) {
    const gap = 14;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Try to place above the target; if not enough room, place below
    _tourTooltip.style.left = '';
    _tourTooltip.style.right = '';
    _tourTooltip.style.top = '';
    _tourTooltip.style.bottom = '';

    // Measure tooltip
    _tourTooltip.style.visibility = 'hidden';
    _tourTooltip.style.display = 'block';
    const ttRect = _tourTooltip.getBoundingClientRect();
    _tourTooltip.style.visibility = '';

    const centerX = targetRect.left + targetRect.width / 2;
    let left = centerX - ttRect.width / 2;
    left = Math.max(12, Math.min(left, vw - ttRect.width - 12));
    _tourTooltip.style.left = left + 'px';

    if (targetRect.top - ttRect.height - gap > 10) {
      // Above
      _tourTooltip.style.top = (targetRect.top - ttRect.height - gap) + 'px';
    } else {
      // Below
      _tourTooltip.style.top = (targetRect.bottom + gap) + 'px';
    }
  }

  function advanceTour() {
    _tourStep++;
    if (_tourStep >= TOUR_STEPS.length) { endTour(); return; }
    showTourStep();
  }

  function endTour() {
    localStorage.setItem('aid_tour_done', '1');
    _tourOverlay?.remove();
    _tourSpotlight?.remove();
    _tourTooltip?.remove();
    _tourOverlay = _tourSpotlight = _tourTooltip = null;
  }

  function updateOnboardingHint() {
    // No-op — onboarding is now handled by the tour
  }

  // ---- Upload + compression ----
  const DIRECT_UPLOAD_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp']);
  // Above this, route through canvas downscale (max side 2048). The image
  // model works at ≤1536px, so a 4MB 12MP phone photo is pure upload cost —
  // and quick mode re-uploads the same product image once per card.
  const DIRECT_UPLOAD_MAX_BYTES = 1.5 * 1024 * 1024;

  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => resolve(e.target.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  function compressImage(file) {
    if (DIRECT_UPLOAD_TYPES.has(file.type) && file.size <= DIRECT_UPLOAD_MAX_BYTES) {
      return readFileAsDataUrl(file);
    }
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => {
        const img = new Image();
        img.onload = () => {
          const canvas = document.createElement('canvas');
          let width = img.width;
          let height = img.height;
          const max_size = 2048;
          if (width > height) {
            if (width > max_size) { height *= max_size / width; width = max_size; }
          } else {
            if (height > max_size) { width *= max_size / height; height = max_size; }
          }
          canvas.width = width;
          canvas.height = height;
          const ctx = canvas.getContext('2d');
          // White underlay: JPEG has no alpha, and transparent-background PNG
          // cutouts (common for product shots) would otherwise turn black.
          ctx.fillStyle = '#fff';
          ctx.fillRect(0, 0, width, height);
          ctx.drawImage(img, 0, 0, width, height);
          resolve(canvas.toDataURL('image/jpeg', 0.95));
        };
        img.onerror = reject;
        img.src = e.target.result;
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }
  function imagePayloadFromDataUrl(dataUrl) {
    const [header, b64] = dataUrl.split(',');
    const mimeMatch = header.match(/^data:(.*?);base64$/);
    return { type: 'base64', mime: mimeMatch ? mimeMatch[1] : 'image/jpeg', data: b64 };
  }
  function fetchImageAsDataUrl(url) {
    return fetch(url).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.blob();
    }).then(blob => new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = reject;
      fr.readAsDataURL(blob);
    }));
  }

  // Original images carry product identity and photographic style directly.
  const PRODUCT_PROMPT = 'Image 1 is the product. Preserve its design, colors, fabric, patterns and prints faithfully.';
  const STYLE_REF_PROMPT = [
    PRODUCT_PROMPT,
    'Image 2 is a photography reference only; do not transfer its clothing or product details.',
    'Create a new commercial photograph of Image 1’s product, inspired by Image 2’s light and shadow, composition and overall aesthetic.',
    'Choose poses and scene details freely. Photorealistic; no added text or watermarks. Match the requested aspect ratio.',
  ].join(' ');

  const SHOT_INTENT_HINTS = {
    worn_full_body: 'Show the product worn by a model, with full-body framing.',
    worn_half_body: 'Show the product worn by a model, with half-body framing.',
    hanger: 'Show the garment on a hanger.',
    print_detail: 'Make a close-up of the product details.',
    editorial_lifestyle: 'Show the product naturally worn in a lifestyle photograph.',
    studio_editorial: 'Show the product worn in a studio editorial photograph.',
  };

  // Only needed when the reference image is unavailable. Bound older specs
  // and omit wardrobe, prescribed gestures, props and detailed staging.
  function buildStyleDirective(styleSpec) {
    const spec = styleSpec || {};
    return ['lighting', 'camera', 'composition', 'mood'].map(key => {
      const value = String(spec[key] || '').trim().split(/\s+/).slice(0, 10).join(' ').slice(0, 80);
      return value ? `${key}: ${value}` : '';
    }).filter(Boolean).join('; ');
  }

  function buildStyleRefPrompt(template) {
    const intentHint = SHOT_INTENT_HINTS[template.shotIntent] || 'Show wearable products naturally worn by a model.';
    if (template.sampleImage) return [STYLE_REF_PROMPT, intentHint].join('\n');
    return [
      PRODUCT_PROMPT,
      'Create a new photorealistic commercial photograph. Choose poses and scene details freely; no added text or watermarks. Match the requested aspect ratio.',
      intentHint,
      buildStyleDirective(template.styleSpec),
    ].filter(Boolean).join('\n');
  }

  function buildRegenerationPrompt(prompt) {
    const basePrompt = String(prompt || '').split('\n\nVariation request:')[0];
    return [basePrompt, 'Variation request: create another interpretation with the same product and photographic feel.'].join('\n\n');
  }
  async function handleUpload(file) {
    if (!file || !file.type.startsWith('image/')) return;
    const t0 = performance.now();
    try {
      refImage = await compressImage(file);
    } catch (e) {
      // HEIC / exotic formats fail canvas decode silently otherwise — the
      // first step of the funnel must never be a dead end.
      console.warn('[aid] upload decode failed:', e);
      toast('图片无法读取，请改用 JPG/PNG 格式重试', 'error');
      return;
    }
    console.info('[TIMING frontend] image_compress=%dms compressed_b64=%dKB',
      Math.round(performance.now() - t0), Math.round(refImage.length / 1024));
    updateDockUploadButton();
    updateOnboardingHint();
    refreshQuickPanelState();
    toast('产品图已就绪,点任意风格即可生成');
  }
  function updateDockUploadButton() {
    const btn = $('#dock-upload');
    if (!btn) return;
    const svg = btn.querySelector('svg');
    let img = btn.querySelector('.dock-thumb');
    let clearBtn = btn.querySelector('.dock-thumb-clear');
    if (refImage) {
      if (!img) {
        img = document.createElement('img');
        img.className = 'dock-thumb';
        img.alt = '已上传产品图';
        btn.appendChild(img);
      }
      img.src = refImage;
      if (svg) svg.style.display = 'none';
      if (!clearBtn) {
        clearBtn = document.createElement('span');
        clearBtn.className = 'dock-thumb-clear';
        clearBtn.title = '清除产品图';
        clearBtn.setAttribute('role', 'button');
        clearBtn.innerHTML = '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';
        clearBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          e.preventDefault();
          refImage = null;
          updateDockUploadButton();
          updateOnboardingHint();
          refreshQuickPanelState();
          toast('已清除产品图');
        });
        btn.appendChild(clearBtn);
      }
    } else {
      if (img) img.remove();
      if (clearBtn) clearBtn.remove();
      if (svg) svg.style.display = '';
    }
  }

  // ---- API ----
  // Friendly Chinese hints for common HTTP failure modes. Reverse-proxy/CDN
  // edge errors arrive as HTML pages, which would otherwise blow up
  // resp.json() and surface a useless "Unexpected token '<'" error.
  const STATUS_HINTS = {
    429: '请求过于频繁，请稍后再试',
    500: '服务器内部错误，请重新生成',
    502: '服务器网关错误，请重新生成',
    503: '服务暂时不可用，请稍后重试',
    504: '生成超时（图片可能仍在处理），请稍后重新生成',
  };
  async function apiCall(path, body) {
    const headers = {
      'Content-Type': 'application/json',
      'ngrok-skip-browser-warning': 'true',
    };
    if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
    let resp;
    try {
      resp = await fetch(path, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
      });
    } catch (netErr) {
      throw new Error('网络连接失败，请检查网络后重试');
    }
    if (resp.status === 401) {
      localStorage.removeItem('aid_token');
      authToken = '';
      showLoginOverlay('登录已失效，请重新登录');
      throw new Error('登录已失效');
    }
    const text = await resp.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) { /* handled below */ }
    }
    if (!resp.ok) {
      if (data && data.error) throw new Error(data.error);
      throw new Error(STATUS_HINTS[resp.status] || `服务异常 (${resp.status})，请重新生成`);
    }
    if (data === null) throw new Error('服务返回为空，请重新生成');
    return data;
  }
  async function apiGet(path) {
    const headers = { 'ngrok-skip-browser-warning': 'true' };
    if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
    let resp;
    try {
      resp = await fetch(path, { headers });
    } catch (netErr) {
      throw new Error('网络连接失败，请检查网络后重试');
    }
    if (resp.status === 401) {
      localStorage.removeItem('aid_token');
      authToken = '';
      showLoginOverlay('登录已失效，请重新登录');
      throw new Error('登录已失效');
    }
    const text = await resp.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) {}
    }
    if (!resp.ok) {
      if (data && data.error) throw new Error(data.error);
      throw new Error(STATUS_HINTS[resp.status] || `服务异常 (${resp.status})，请重新生成`);
    }
    return data || {};
  }

  // ---- Categories + Templates ----
  function inferDynamicStyleName(styleSpec, index) {
    const spec = styleSpec || {};
    const text = [
      spec.shotType,
      spec.lighting,
      spec.palette,
      spec.composition,
      spec.styling,
      spec.mood,
      spec.sceneDirection,
      spec.commercialIntent,
      spec.variationRules,
    ].filter(Boolean).join(' ').toLowerCase();

    if (/\b(beach|ocean|coastal|resort|palm|seaside)\b/.test(text)) return '海边度假';
    if (/\b(subway|metro|train|commute)\b/.test(text)) return '地铁通勤';
    if (/\b(street|urban|metropolitan|crosswalk|sidewalk)\b/.test(text)) return '都市街拍';
    if (/\b(countryside|barn|fence|grass|pickup|rural)\b/.test(text)) return '美式复古';
    if (/\b(home|interior|room|window|cafe|reading|chair)\b/.test(text)) return '居家文艺';
    if (/\b(studio|seamless|backdrop|packshot)\b/.test(text)) return '棚拍上身';
    if (/\b(preppy|varsity|campus|college)\b/.test(text)) return '学院休闲';
    if (/\b(sport|athletic|jersey)\b/.test(text)) return '运动休闲';
    if (/\b(motion|walking|in-motion)\b/.test(text)) return '动态街拍';
    if (/\b(full-length|full body|full-body)\b/.test(text)) return '全身上身';
    if (/\b(lookbook|on-model|product-on-model)\b/.test(text)) return '模特上身';
    if (/\b(portrait|half-body|mid-length|cropped|waist-up)\b/.test(text)) return '半身上身';
    return `灵感参考 ${String(index + 1).padStart(2, '0')}`;
  }
  // All style cards now come from /api/style-assets. The legacy static list
  // in templates.js has been retired — categories live here, assets live in
  // SQLite (hydrated from style_assets.json on startup).
  const AID_CATEGORIES = [
    { id: 'apparel', name: '服饰' },
  ];

  const VALID_SHOT_INTENTS = new Set([
    'worn_full_body', 'worn_half_body', 'hanger',
    'print_detail', 'editorial_lifestyle', 'studio_editorial',
  ]);

  function dynamicAssetToTemplate(asset, index) {
    const styleSpec = asset.styleSpec || null;
    const llmLabel = (styleSpec && typeof styleSpec.chineseLabel === 'string')
      ? styleSpec.chineseLabel.trim() : '';
    const name = llmLabel || inferDynamicStyleName(styleSpec, index);
    const llmShotIntent = (styleSpec && styleSpec.shotIntent || '').trim();
    const shotIntent = VALID_SHOT_INTENTS.has(llmShotIntent) ? llmShotIntent : 'editorial_lifestyle';
    return {
      id: asset.id || `style-asset-${index}`,
      name: name,
      subtitle: '灵感参考',
      category: 'apparel',
      shotIntent: shotIntent,
      sampleImage: asset.sampleImage || '',
      // Small 400px WebP for gallery cards; sampleImage (full-res) stays the
      // reference sent to the image model. Falls back to the original if the
      // server didn't supply a thumbnail.
      thumbImage: asset.thumbImage || asset.sampleImage || '',
      styleSpec: styleSpec,
      // Leave cardRatio unset so cardAspectRatio() can derive it from
      // shotIntent (3/4 for half-body / studio, 2/3 default). This brings
      // some real height variation back to the masonry.
      dynamic: true,
    };
  }
  function getCategories() {
    return [...AID_CATEGORIES];
  }
  function getTemplatesForCategory(catId) {
    if (catId !== 'apparel') return [];
    return dynamicStyleAssets
      .map(dynamicAssetToTemplate)
      .filter(t => !isBlockedStyleImage(t.sampleImage));
  }
  function templateNameCounts(tpls) {
    return tpls.reduce((counts, t) => {
      const name = t.name || '';
      if (name) counts.set(name, (counts.get(name) || 0) + 1);
      return counts;
    }, new Map());
  }
  function templateCardMeta(t, counts) {
    if (t.dynamic) {
      return {
        title: t.name || '灵感参考',
        subtitle: t.subtitle || t.commerceUse || '点击生成',
        generationName: t.name || '灵感参考',
      };
    }
    const duplicateName = (counts?.get(t.name || '') || 0) > 1;
    const detail = t.subtitle || '';
    if (duplicateName && detail) {
      return {
        title: t.name,
        subtitle: detail,
        generationName: `${t.name} · ${detail}`,
      };
    }
    return {
      title: t.name || '灵感参考',
      subtitle: t.commerceUse || t.subtitle || '',
      generationName: t.name || '灵感参考',
    };
  }
  function templateGenerationName(template) {
    if (template.displayName) return template.displayName;
    if (template.dynamic) return template.name || '灵感参考';
    const name = template.name || '灵感参考';
    const detail = template.subtitle || '';
    if (!detail) return name;
    for (const cat of getCategories()) {
      const sameNameCount = getTemplatesForCategory(cat.id)
        .filter(t => (t.name || '') === name).length;
      if (sameNameCount > 1) return `${name} · ${detail}`;
    }
    return name;
  }
  function isTemplateFavorited(templateId) {
    return favoriteTemplates.some(t => t.id === templateId);
  }
  function templateSnapshot(template) {
    return {
      id: template.id,
      name: template.name || '灵感参考',
      subtitle: template.subtitle || '',
      category: template.category || currentCategoryId || 'apparel',
      shotIntent: template.shotIntent || 'editorial_lifestyle',
      sampleImage: template.sampleImage || '',
      thumbImage: template.thumbImage || template.sampleImage || '',
      styleSpec: template.styleSpec || null,
      cardRatio: template.cardRatio || '2 / 3',
      dynamic: !!template.dynamic,
      displayName: templateGenerationName(template),
      savedAt: Date.now(),
    };
  }
  function toggleFavoriteTemplate(template) {
    const idx = favoriteTemplates.findIndex(t => t.id === template.id);
    const isAdd = idx < 0;
    if (isAdd) {
      favoriteTemplates.unshift(templateSnapshot(template));
      saveFavoriteTemplates();
      toast('已收藏到我的收藏');
    } else {
      favoriteTemplates.splice(idx, 1);
      saveFavoriteTemplates();
      toast('已取消收藏');
    }
    // Optimistic UI: bump local count immediately, then sync with server.
    const current = favoriteCounts[template.id] ?? 0;
    favoriteCounts[template.id] = Math.max(0, current + (isAdd ? 1 : -1));
    renderTemplatesForCategory(currentCategoryId);
    renderFavoriteGrid();
    apiCall('/api/favorite-toggle', { template_id: template.id, action: isAdd ? 'add' : 'remove' })
      .then(data => {
        if (typeof data.count === 'number') {
          favoriteCounts[template.id] = data.count;
          renderTemplatesForCategory(currentCategoryId);
          renderFavoriteGrid();
        }
      })
      .catch(err => console.warn('[aid] favorite-toggle failed:', err));
  }

  async function loadFavoriteCounts(templateIds) {
    if (!templateIds || templateIds.length === 0) return;
    try {
      const data = await apiGet('/api/favorite-counts?ids=' + encodeURIComponent(templateIds.join(',')));
      if (data && data.counts) Object.assign(favoriteCounts, data.counts);
    } catch (err) {
      console.warn('[aid] favorite-counts load failed:', err);
    }
  }
  // ---- Lazy background-image loading ----
  // Style cards render with a placeholder gradient + a data-bg URL; the real
  // image is only fetched once the card scrolls within ~400px of the viewport.
  // This keeps first-load weight roughly constant as the style library grows,
  // instead of firing one image request per card up front.
  const _lazyBgObserver = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries, obs) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const el = entry.target;
          const url = el.dataset.bg;
          if (url) {
            el.style.backgroundImage = `url('${url}')`;
            el.removeAttribute('data-bg');
          }
          obs.unobserve(el);
        }
      }, { rootMargin: '400px 0px' })
    : null;

  function observeLazyBg(container) {
    if (!container) return;
    const els = container.querySelectorAll('[data-bg]');
    if (!_lazyBgObserver) {
      // Old browser without IntersectionObserver: load everything immediately.
      els.forEach(el => {
        const url = el.dataset.bg;
        if (url) { el.style.backgroundImage = `url('${url}')`; el.removeAttribute('data-bg'); }
      });
      return;
    }
    els.forEach(el => _lazyBgObserver.observe(el));
  }

  function cardAspectRatio(t) {
    if (t.cardRatio) return t.cardRatio;
    if (t.shotIntent === 'print_detail') return '1 / 1';
    if (t.shotIntent === 'hanger') return '4 / 5';
    if (t.shotIntent === 'worn_half_body' || t.shotIntent === 'studio_editorial') return '3 / 4';
    return '2 / 3';
  }
  function renderCategories() {
    const cats = getCategories();
    const wrap = $('#category-tabs');
    if (!wrap) return;
    // Hide tab bar when there's only one category (no need to switch)
    if (cats.length <= 1) {
      wrap.style.display = 'none';
    } else {
      wrap.style.display = '';
      const html = cats.map(c => `<button class="category-tab" data-cat-id="${c.id}">${escapeHtml(c.name)}</button>`).join('');
      wrap.innerHTML = html;
      document.querySelectorAll('.category-tab').forEach(tab => {
        tab.addEventListener('click', () => selectCategory(tab.dataset.catId));
      });
    }
  }
  function selectCategory(catId) {
    currentCategoryId = catId;
    document.querySelectorAll('.category-tab').forEach(tab => {
      tab.classList.toggle('active', tab.dataset.catId === catId);
    });
    renderTemplatesForCategory(catId);
  }
  function renderTemplatesForCategory(catId) {
    const all = getTemplatesForCategory(catId);
    // When a header search is active, filter the gallery in place so the
    // keyword-matching style cards stay on the page (we removed the old
    // dropdown — the results ARE the gallery). Empty query → show everything.
    const tpls = searchQuery ? all.filter(t => templateMatchesQuery(t, searchQuery)) : all;
    renderTemplateCards(tpls);
  }

  function renderTemplateCards(tpls) {
    const grid = $('#template-grid');
    if (!grid) return;
    // Curated cards and Pinterest cards must stay in the same multicol
    // container. Preserve the live pin nodes (and their click handlers) when
    // category/search/favorite changes re-render just the curated portion.
    const pinCards = Array.from(grid.children)
      .filter(el => el.classList.contains('pin-card'));
    const restorePinCards = () => pinCards.forEach(card => grid.appendChild(card));
    if (searchQuery && tpls.length === 0) {
      // With the Pinterest feed live below, "no local matches" isn't a dead
      // end — the same query is (or is about to be) searched on Pinterest.
      const hint = feedAvailable()
        ? `本地风格没有匹配「${escapeHtml(searchQuery)}」，看看下面来自 Pinterest 的结果 ↓`
        : `没有匹配「${escapeHtml(searchQuery)}」的风格，换个关键词试试。`;
      grid.innerHTML = `<div class="gallery-empty">${hint}</div>`;
      restorePinCards();
      return;
    }
    const counts = templateNameCounts(tpls);
    const viewTpls = tpls.map(t => {
      const meta = templateCardMeta(t, counts);
      return { ...t, displayName: meta.generationName, cardTitle: meta.title, cardSubtitle: meta.subtitle };
    });
    const html = viewTpls.map(t => {
      const hasSample = !!t.sampleImage;
      const favActive = isTemplateFavorited(t.id);
      const favTitle = favActive ? '取消收藏' : '收藏风格';
      const styleAttr = hasSample
        ? `background-image:linear-gradient(135deg,#2a2a2a,#404040);background-size:cover;background-position:center;color:#fff;aspect-ratio:${cardAspectRatio(t)};`
        : `background:linear-gradient(135deg,#2a2a2a,#404040);color:#fff;aspect-ratio:${cardAspectRatio(t)};`;
      const favCount = favoriteCounts[t.id];
      const countHtml = (typeof favCount === 'number') ? `<span class="fav-count">${favCount}</span>` : '';
      const bgAttr = hasSample ? ` data-bg="${t.thumbImage || t.sampleImage}"` : '';
      return `
        <div class="template-card${hasSample ? ' has-sample' : ''}" data-tpl-id="${t.id}"${bgAttr} style="${styleAttr}">
          <button class="template-fav-btn${favActive ? ' active' : ''}" data-tpl-id="${t.id}" title="${favTitle}" aria-label="${favTitle}">${ICON_HEART}${countHtml}</button>
          <div class="tc-meta">
            <div class="tc-name">${escapeHtml(t.cardTitle)}</div>
            <div class="tc-subtitle">${escapeHtml(t.cardSubtitle)}</div>
          </div>
        </div>
      `;
    }).join('');
    grid.innerHTML = html;
    restorePinCards();
    observeLazyBg(grid);
    // Scope handlers to the gallery grid — the favorites grid also uses
    // .template-card and must keep its own click behavior.
    grid.querySelectorAll('.template-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('.template-fav-btn')) return;
        const tpl = viewTpls.find(t => t.id === card.dataset.tplId);
        if (tpl) startGeneration(tpl);
      });
    });
    grid.querySelectorAll('.template-card .template-fav-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const tpl = viewTpls.find(t => t.id === btn.dataset.tplId);
        if (tpl) toggleFavoriteTemplate(tpl);
      });
    });
  }
  function renderTemplates() {
    renderCategories();
    const cats = getCategories();
    const activeCat = cats.find(c => c.id === currentCategoryId) || cats[0];
    if (activeCat) selectCategory(activeCat.id);
  }
  async function loadDynamicStyleAssets() {
    try {
      const data = await apiGet('/api/style-assets');
      dynamicStyleAssets = Array.isArray(data.assets) ? data.assets : [];
      pruneStaleFavorites();
      renderTemplates();
      renderFavoriteGrid();
    } catch (err) {
      console.warn('[aid] style assets load failed:', err);
    }
  }

  function pruneStaleFavorites() {
    // Drop favorites whose sampleImage is no longer in the current style
    // library — happens when the operator wipes/replaces the asset set.
    // Without this, the favorite grid shows black cards (broken bg-image).
    if (!Array.isArray(favoriteTemplates) || favoriteTemplates.length === 0) return;
    const live = new Set(dynamicStyleAssets.map(a => a.sampleImage).filter(Boolean));
    const kept = favoriteTemplates.filter(t => !t.sampleImage || live.has(t.sampleImage));
    if (kept.length !== favoriteTemplates.length) {
      favoriteTemplates = kept;
      saveFavoriteTemplates();
    }
  }

  // ---- Background generation ----
  // refOverride: when set, use this data URL instead of the global refImage.
  // Used by "重新生成" so the user doesn't have to re-upload to retry an item.
  // styleRefUrl: same-origin URL to a card's sampleImage. When set, the
  // generation goes through dual-image mode (product + style reference).
  async function enqueueGen({ name, prompt, size, refOverride, styleRefUrl, styleSpec, shotIntent }) {
    // Guard: if the login overlay is currently shown, the user shouldn't be
    // dispatching jobs. Prevents a 401 storm when N quick-mode jobs fire
    // before the user has logged in.
    const loginOverlay = $('#login-overlay');
    if (loginOverlay && !loginOverlay.classList.contains('hidden')) {
      toast('请先登录', 'error');
      return;
    }
    const usedRef = refOverride || refImage;
    if (!usedRef) {
      toast('请先点 + 上传产品图', 'error');
      return;
    }
    const usedSize = RATIO_SIZES.has(size) ? size : currentRatio;
    const item = {
      id: uniqueId(),
      refDataUrl: usedRef,
      prompt: prompt,
      styleRefUrl: styleRefUrl || null,
      styleSpec: styleSpec || null,
      shotIntent: shotIntent || null,
      templateName: name,
      size: usedSize,
      status: 'loading',
      resultBase64: '',
      errMsg: '',
      createdAt: Date.now(),
      seen: false,
    };
    myItems.unshift(item);
    trimMyItems();
    saveItem(item);
    renderMyGrid();
    updateDockBadges();
    toast(`${name} 已加入生成`);

    // Note: refImage is intentionally kept so the user can fan out one upload
    // across multiple style cards in parallel. They clear it manually via the
    // × button on the dock thumbnail, or by uploading a new image.

    // Send both originals to the image model. The server uses only the style
    // reference for short photography guidance from the auxiliary model.
    const artDirect = !!styleSpec;
    const images = [imagePayloadFromDataUrl(usedRef)];
    let styleThumbPayload = null;
    if (styleRefUrl) {
      try {
        const styleDataUrl = await fetchImageAsDataUrl(styleRefUrl);
        images.push(imagePayloadFromDataUrl(styleDataUrl));
      } catch (e) {
        // Submitting without the style image would generate a barely-edited
        // flat-lay AND burn quota — abort instead, retry is one tap away.
        console.warn('[aid] style ref fetch failed:', e);
        finishItem(item, null, '风格图加载失败，请点「换一张」重试');
        return;
      }
      // The 400px WebP thumbnail (already cached by the gallery) is plenty
      // for the server's brief-writing LLM — sending it halves that call's
      // vision cost. Best-effort: the server falls back to the full image.
      const thumbUrl = styleRefUrl.replace(/\.(jpe?g|png)$/i, '.t400.webp');
      if (thumbUrl !== styleRefUrl) {
        try {
          styleThumbPayload = imagePayloadFromDataUrl(await fetchImageAsDataUrl(thumbUrl));
        } catch (e) { /* fall back to full-size server-side */ }
      }
    }

    const body = {
      prompt: prompt,
      size: usedSize,
      image: images,
    };
    if (artDirect) {
      body.art_direct = true;
      body.style_spec = styleSpec;
      if (shotIntent) body.shot_intent = shotIntent;
      if (styleThumbPayload) body.style_thumb_image = styleThumbPayload;
    }

    // Submit as a server-side job: the server returns a job id immediately
    // and generates in the background, so the image survives tab switches,
    // phone lock and page reloads — we just poll for the result.
    apiCall('/api/generate-async', body).then(data => {
      const cur = myItems.find(x => x.id === item.id);
      if (!cur) return;
      if (!data.job_id) {
        finishItem(cur, null, '任务提交失败，请重试');
        return;
      }
      cur.jobId = data.job_id;
      saveItem(cur);
      ensureJobPolling();
      // remaining=-1 → unlimited (admin/local). In accounts mode it's the live
      // credit balance; in legacy mode it's the daily per-IP quota.
      if (typeof data.remaining === 'number') {
        if (accountsMode) {
          setCreditDisplay(data.remaining);
          if (data.remaining >= 0 && data.remaining <= 3) toast(`剩余额度 ${data.remaining}`);
        } else if (data.remaining >= 0 && data.remaining <= 5) {
          toast(`今日还可生成 ${data.remaining} 张`);
        }
      }
    }).catch(err => {
      const cur = myItems.find(x => x.id === item.id);
      if (!cur) return;
      const msg = err.message || String(err);
      // Quota/credit exhaustion must be visible where the user is (the home
      // grid), not only as an error card buried in「我的」.
      if (/额度不足|次数已用完|限额|频繁/.test(msg)) toast(msg, 'error');
      if (accountsMode && /额度不足/.test(msg)) { setCreditDisplay(0); openRecharge(); }
      finishItem(cur, null, msg);
    });
  }

  // ---- Job polling ----
  // Generation now runs server-side; we poll all in-flight jobs on one timer.
  const JOB_POLL_MS = 4000;
  // Must exceed the server's worst case (brief LLM 2×60s + two 600s upstream
  // attempts + 300s task polling ≈ 21-22 min) — giving up earlier discards
  // images that actually finished.
  const JOB_TIMEOUT_MS = 22 * 60 * 1000;
  let _jobPollTimer = null;
  let _jobPollBusy = false;
  let _jobPollTick = 0;

  function ensureJobPolling() {
    if (_jobPollTimer) return;
    if (!myItems.some(it => it.status === 'loading' && it.jobId)) return;
    _jobPollTimer = setInterval(pollPendingJobs, JOB_POLL_MS);
    pollPendingJobs();
  }

  async function pollPendingJobs(force) {
    if (_jobPollBusy) return;
    const pending = myItems.filter(it => it.status === 'loading' && it.jobId);
    if (!pending.length) {
      if (_jobPollTimer) { clearInterval(_jobPollTimer); _jobPollTimer = null; }
      return;
    }
    // Light backoff: once every job has been running >2 min, poll every third
    // tick (~12s) — long generations don't need a 4s cadence. `force` (used
    // when the tab returns to the foreground) skips the backoff.
    _jobPollTick++;
    const allOld = pending.every(it => Date.now() - (it.createdAt || 0) > 120000);
    if (!force && allOld && _jobPollTick % 3 !== 0) return;
    _jobPollBusy = true;
    try {
      await Promise.all(pending.map(async (item) => {
        const expired = Date.now() - (item.createdAt || 0) > JOB_TIMEOUT_MS;
        let data;
        try {
          data = await apiGet('/api/generate-result/' + item.jobId);
        } catch (e) {
          // Transient network error (or 401 → login overlay already shown).
          // Keep the item loading and retry next tick, unless it's way past
          // any plausible generation time.
          if (expired) finishItem(item, null, '生成超时，请重新生成');
          return;
        }
        if (data.status === 'done' && data.image) {
          finishItem(item, data.image, '', data.brief || '');
        } else if (data.status === 'error') {
          finishItem(item, null, data.error || '生成失败，请重新生成');
        } else if (data.status === 'not_found') {
          finishItem(item, null, '这张图需要重新生成一下');
        } else if (expired) {
          finishItem(item, null, '生成超时，请重新生成');
        }
      }));
    } finally {
      _jobPollBusy = false;
    }
  }

  function finishItem(item, image, errMsg, brief) {
    const cur = myItems.find(x => x.id === item.id);
    if (!cur) return;
    if (image) {
      cur.status = 'done';
      cur.resultBase64 = image;
      // Retain the photography guidance for this result, without feeding it back.
      if (brief) cur.brief = brief;
    } else {
      cur.status = 'error';
      cur.errMsg = errMsg;
    }
    // If the user is already on "my" when this finishes, auto-mark seen.
    if (currentView === 'my' && currentMyTab === 'gallery') cur.seen = true;
    saveItem(cur);
    updateMyCard(cur);
    updateDockBadges();
  }

  function startGeneration(template) {
    // The server adds concise photography guidance; both original images
    // remain available to the image model.
    enqueueGen({
      name: templateGenerationName(template),
      prompt: buildStyleRefPrompt(template),
      size: currentRatio,
      styleRefUrl: template.sampleImage || null,
      styleSpec: template.styleSpec || null,
      shotIntent: template.shotIntent || null,
    });
  }

  function regenerateItem(itemId) {
    const item = myItems.find(x => x.id === itemId);
    if (!item || !item.refDataUrl || !item.prompt) {
      toast('无法重新生成: 数据不完整', 'error');
      return;
    }
    enqueueGen({
      name: item.templateName || '换一张',
      prompt: buildRegenerationPrompt(item.styleRefUrl || item.styleSpec
        ? buildStyleRefPrompt({ sampleImage: item.styleRefUrl, styleSpec: item.styleSpec, shotIntent: item.shotIntent })
        : item.prompt),
      size: item.size || currentRatio,
      refOverride: item.refDataUrl,
      styleRefUrl: item.styleRefUrl || null,
      // Reuse the reference and broad photography direction on regeneration.
      styleSpec: item.styleSpec || null,
      shotIntent: item.shotIntent || null,
    });
  }

  function deleteItem(itemId) {
    const idx = myItems.findIndex(x => x.id === itemId);
    if (idx === -1) return;
    myItems.splice(idx, 1);
    dbDelete(itemId).catch(err => console.warn('[aid] db delete failed:', err));
    renderMyGrid();
    updateDockBadges();
  }
  function markGallerySeen() {
    myItems.forEach(it => {
      if (it.status !== 'loading' && !it.seen) { it.seen = true; saveItem(it); }
    });
    updateDockBadges();
  }

  // ---- My grid ----
  const ICON_DL = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
  const ICON_REGEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>';
  const ICON_DEL = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg>';

  const CARD_ASPECTS = { '1024x1536': '2/3', '1024x1024': '1/1', '1536x1024': '3/2' };

  function myCardHTML(item) {
    // Match the card shape to the generated aspect ratio — square/landscape
    // results inside the default portrait card look cropped, as if the ratio
    // setting didn't work.
    const aspect = CARD_ASPECTS[item.size] || '';
    const aspectStyle = aspect ? ` style="aspect-ratio:${aspect}"` : '';
    let inner;
    if (item.status === 'loading') {
      inner = `<div class="my-card-loading">
          <div class="loader-dots"><span></span><span></span><span></span></div>
          <div class="my-card-stage" data-created="${item.createdAt || Date.now()}"></div>
          <div class="my-card-tpl">${escapeHtml(item.templateName)}</div>
        </div>
        <div class="my-card-actions">
          <button class="img-action-btn btn-del" data-id="${item.id}" title="删除">${ICON_DEL}</button>
        </div>`;
    } else if (item.status === 'error') {
      inner = `<div class="my-card-error">
          <div class="my-card-error-title">${escapeHtml(item.templateName)} 失败</div>
          <div class="my-card-error-msg">${escapeHtml(item.errMsg || '未知错误')}</div>
        </div>
        <div class="my-card-actions">
          <button class="img-action-btn btn-regen" data-id="${item.id}" title="换一张">${ICON_REGEN}</button>
          <button class="img-action-btn btn-del" data-id="${item.id}" title="删除">${ICON_DEL}</button>
        </div>`;
    } else {
      // data-src + IntersectionObserver: decoding every multi-MB result at
      // once froze the main thread as the gallery grew.
      inner = `<img data-src="data:image/png;base64,${item.resultBase64}" alt="${escapeHtml(item.templateName)}" data-id="${item.id}" />
        <div class="my-card-label">${escapeHtml(item.templateName)}</div>
        <div class="my-card-actions">
          <button class="img-action-btn btn-regen" data-id="${item.id}" title="换一张">${ICON_REGEN}</button>
          <button class="img-action-btn btn-dl" data-id="${item.id}" title="下载">${ICON_DL}</button>
          <button class="img-action-btn btn-del" data-id="${item.id}" title="删除">${ICON_DEL}</button>
        </div>`;
    }
    return `<div class="my-card" data-id="${item.id}"${aspectStyle}>${inner}</div>`;
  }

  let _myImgObserver = null;
  function observeLazyMyImgs(root) {
    if (!('IntersectionObserver' in window)) {
      root.querySelectorAll('img[data-src]').forEach(img => { img.src = img.dataset.src; delete img.dataset.src; });
      return;
    }
    if (!_myImgObserver) {
      _myImgObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          if (!entry.isIntersecting) return;
          const img = entry.target;
          if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src; }
          _myImgObserver.unobserve(img);
        });
      }, { rootMargin: '300px' });
    }
    root.querySelectorAll('img[data-src]').forEach(img => _myImgObserver.observe(img));
  }

  function base64ToBlob(b64, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  async function downloadItem(itemId) {
    const it = myItems.find(x => x.id === itemId);
    if (!it || !it.resultBase64) return;
    const filename = `aid_${String(it.templateName || 'image').replace(/[\\/:*?"<>|]/g, '_')}_${it.id}.png`;
    let blob;
    try {
      blob = base64ToBlob(it.resultBase64, 'image/png');
    } catch (e) {
      toast('图片数据异常，无法下载', 'error');
      return;
    }
    // On touch devices the Web Share sheet is the only path that reaches the
    // photo album (iOS Safari ignores the download attribute on blob/data URLs
    // in standalone contexts and never offers "存储图像").
    if (window.matchMedia('(hover: none)').matches && navigator.canShare) {
      try {
        const file = new File([blob], filename, { type: 'image/png' });
        if (navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file] });
          return;
        }
      } catch (e) {
        if (e && e.name === 'AbortError') return; // user closed the sheet
        // fall through to anchor download
      }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  // One delegated listener instead of rebinding per-card handlers on every
  // render — required for single-card DOM updates (updateMyCard).
  function bindMyGridEvents() {
    const grid = $('#my-grid');
    if (!grid || grid._aidBound) return;
    grid._aidBound = true;
    grid.addEventListener('click', (e) => {
      const dl = e.target.closest('.btn-dl');
      if (dl) { e.stopPropagation(); downloadItem(dl.dataset.id); return; }
      const rg = e.target.closest('.btn-regen');
      if (rg) { e.stopPropagation(); regenerateItem(rg.dataset.id); return; }
      const del = e.target.closest('.btn-del');
      if (del) {
        e.stopPropagation();
        if (confirm('确定删除这张作品?')) deleteItem(del.dataset.id);
        return;
      }
      const img = e.target.closest('img[data-id]');
      if (img) { e.stopPropagation(); openLightbox(img.src || img.dataset.src, img.dataset.id); }
    });
  }

  function renderMyGrid() {
    const grid = $('#my-grid');
    if (!grid) return;
    bindMyGridEvents();
    if (!myItems.length) {
      grid.innerHTML = '<div class="my-empty">还没有作品。上传产品图，选个风格，作品会出现在这里。</div>';
      return;
    }
    grid.innerHTML = myItems.map(myCardHTML).join('');
    observeLazyMyImgs(grid);
    startLoadingTimers();
  }

  // Replace a single card in place — a finishing job must not rebuild (and
  // re-decode) the entire grid of base64 images.
  function updateMyCard(item) {
    const grid = $('#my-grid');
    if (!grid) return;
    const card = grid.querySelector(`.my-card[data-id="${item.id}"]`);
    if (!card) { renderMyGrid(); return; }
    const wrap = document.createElement('div');
    wrap.innerHTML = myCardHTML(item);
    const fresh = wrap.firstElementChild;
    card.replaceWith(fresh);
    observeLazyMyImgs(fresh);
    startLoadingTimers();
  }

  function favoriteTemplateForDisplay(saved) {
    const live = findTemplateById(saved.id);
    return live ? { ...saved, ...live } : saved;
  }

  function renderFavoriteGrid() {
    const grid = $('#favorite-grid');
    if (!grid) return;
    const visibleFavorites = favoriteTemplates;
    grid.classList.toggle('empty', visibleFavorites.length === 0);
    if (!visibleFavorites.length) {
      grid.innerHTML = '<div class="my-empty">还没有收藏。回到主页，点图片右下角的心形按钮收藏风格。</div>';
      return;
    }
    const html = visibleFavorites.map(saved => {
      const t = favoriteTemplateForDisplay(saved);
      const hasSample = !!t.sampleImage;
      const styleAttr = hasSample
        ? `background-image:linear-gradient(135deg,#2a2a2a,#404040);background-size:cover;background-position:center;color:#fff;aspect-ratio:${cardAspectRatio(t)};`
        : `background:linear-gradient(135deg,#2a2a2a,#404040);color:#fff;aspect-ratio:${cardAspectRatio(t)};`;
      const favCount = favoriteCounts[t.id];
      const countHtml = (typeof favCount === 'number') ? `<span class="fav-count">${favCount}</span>` : '';
      const bgAttr = hasSample ? ` data-bg="${t.thumbImage || t.sampleImage}"` : '';
      return `
        <div class="template-card${hasSample ? ' has-sample' : ''}" data-tpl-id="${t.id}"${bgAttr} style="${styleAttr}">
          <button class="template-fav-btn active" data-tpl-id="${t.id}" title="取消收藏" aria-label="取消收藏">${ICON_HEART}${countHtml}</button>
        </div>
      `;
    }).join('');
    grid.innerHTML = html;
    observeLazyBg(grid);
    grid.querySelectorAll('.template-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('.template-fav-btn')) return;
        const saved = visibleFavorites.find(t => t.id === card.dataset.tplId);
        if (saved) startGeneration(favoriteTemplateForDisplay(saved));
      });
    });
    grid.querySelectorAll('.template-fav-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const saved = visibleFavorites.find(t => t.id === btn.dataset.tplId);
        if (saved) toggleFavoriteTemplate(favoriteTemplateForDisplay(saved));
      });
    });
  }

  function renderMyTabs() {
    document.querySelectorAll('.my-tab').forEach(tab => {
      tab.classList.toggle('active', tab.dataset.myTab === currentMyTab);
    });
    const favGrid = $('#favorite-grid');
    const myGrid = $('#my-grid');
    if (favGrid) favGrid.toggleAttribute('hidden', currentMyTab !== 'favorites');
    if (myGrid) myGrid.toggleAttribute('hidden', currentMyTab !== 'gallery');
  }

  function renderMySection() {
    renderMyTabs();
    renderFavoriteGrid();
    renderMyGrid();
  }

  function setMyTab(tab) {
    currentMyTab = tab === 'favorites' ? 'favorites' : 'gallery';
    localStorage.setItem('aid_my_tab_v1', currentMyTab);
    renderMySection();
    if (currentView === 'my' && currentMyTab === 'gallery') markGallerySeen();
  }

  // ---- Loading stage rotation ----
  let _stageInterval = null;
  function startLoadingTimers() {
    const hasLoading = myItems.some(it => it.status === 'loading');
    if (!hasLoading) {
      if (_stageInterval) { clearInterval(_stageInterval); _stageInterval = null; }
      return;
    }
    // Set initial stage text immediately
    updateStageTexts();
    if (_stageInterval) return;
    _stageInterval = setInterval(updateStageTexts, 3000);
  }
  function updateStageTexts() {
    const els = document.querySelectorAll('.my-card-stage[data-created]');
    if (!els.length) { if (_stageInterval) { clearInterval(_stageInterval); _stageInterval = null; } return; }
    const now = Date.now();
    els.forEach(el => {
      const created = parseInt(el.dataset.created, 10) || now;
      const elapsed = Math.floor((now - created) / 1000);
      let text = LOADING_STAGES[0][1];
      for (const [threshold, label] of LOADING_STAGES) {
        if (elapsed >= threshold) text = label;
        else break;
      }
      el.textContent = text;
    });
  }

  // ---- View switching ----
  function syncBodyQuickMode() {
    // Quick-mode visual state (dimmed gallery + locked scroll + hidden dock)
    // only applies when we're on the home view AND the user picked the
    // quick top-mode. Leaving home (e.g. to "my") removes it so the dock
    // and scroll come back.
    const isQuick = currentView === 'home' && currentTopMode === 'quick';
    document.documentElement.classList.toggle('quick-mode', isQuick);
    document.body.classList.toggle('quick-mode', isQuick);
  }

  function setView(view) {
    currentView = view;
    document.querySelectorAll('.dock-btn[data-view]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === view);
    });
    const home = $('#view-home');
    const my = $('#view-my');
    if (view === 'my') {
      if (home) home.setAttribute('hidden', '');
      if (my) my.removeAttribute('hidden');
      renderMySection();
      if (currentMyTab === 'gallery') markGallerySeen();
    } else {
      if (my) my.setAttribute('hidden', '');
      if (home) home.removeAttribute('hidden');
      applyTopMode();  // refresh gallery vs quick when returning to home
    }
    syncBodyQuickMode();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function applyTopMode() {
    const quick = $('#quick-panel');
    document.querySelectorAll('.mode-chip').forEach(c => {
      c.classList.toggle('active', c.dataset.topMode === currentTopMode);
    });
    if (currentTopMode === 'quick') {
      if (quick) quick.hidden = false;
      refreshQuickPanelState();
    } else {
      if (quick) quick.hidden = true;
    }
    syncBodyQuickMode();
  }
  function setTopMode(mode) {
    if (mode !== 'gallery' && mode !== 'quick') return;
    currentTopMode = mode;
    localStorage.setItem('aid_top_mode_v1', mode);
    applyTopMode();
  }
  function refreshQuickPanelState() {
    const zone = $('#quick-upload-zone');
    const clearBtn = $('#quick-upload-clear');
    const btn = $('#quick-generate-btn');
    if (!zone || !btn) return;
    if (refImage) {
      zone.classList.add('has-image');
      zone.style.backgroundImage = `url('${refImage}')`;
      if (clearBtn) clearBtn.hidden = false;
      btn.disabled = false;
    } else {
      zone.classList.remove('has-image');
      zone.style.backgroundImage = '';
      if (clearBtn) clearBtn.hidden = true;
      btn.disabled = true;
    }
  }
  function submitQuickGenerate() {
    if (!refImage) { toast('请先上传产品图', 'error'); return; }
    const pool = dynamicStyleAssets
      .map((a, i) => dynamicAssetToTemplate(a, i))
      .filter(t => t.sampleImage && !isBlockedStyleImage(t.sampleImage));
    if (!pool.length) { toast('风格库为空,无法生成', 'error'); return; }
    // Shuffle and take N
    for (let i = pool.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    const picked = pool.slice(0, Math.min(quickCount, pool.length));
    picked.forEach(t => startGeneration(t));
    toast(`正在生成 ${picked.length} 张`, 'success');
    setView('my');
  }

  function updateDockBadges() {
    const my = $('#dock-my');
    const badge = $('#dock-my-badge');
    if (!my || !badge) return;
    // Loading items are always unread (so the counter persists while images
    // are still being generated, even after the user opens "my"). Completed
    // items are unread until the user opens "my".
    const unread = myItems.filter(it => it.status === 'loading' || !it.seen).length;
    if (unread > 0) {
      my.classList.add('has-badge');
      badge.textContent = unread > 99 ? '99+' : String(unread);
    } else {
      my.classList.remove('has-badge');
      badge.textContent = '';
    }
  }

  // ---- Search (top-right header) ----
  // The style NAME (chineseLabel, e.g. "法式慵懒"/"日系松弛") is the strongest,
  // most distinctive signal and carries most queries on its own — aesthetic
  // words like 法式/日系/街拍/复古/清新/慵懒/森系 all live in the names.
  // The styleSpec prose (mood/palette/styling/…) is English, so it only helps
  // English queries and a few *technical/scene* terms that never appear in the
  // Chinese names (棚拍/户外/黑白/运动/全身…). We map ONLY those, and ONLY to
  // English fragments that are specific enough not to match the whole catalog
  // — broad adjectives like warm/soft/editorial/minimal are deliberately
  // excluded (they appear in 60-80% of descriptions, so they wouldn't filter).
  const SEARCH_SYNONYMS = {
    '棚拍': ['studio', 'backdrop', 'seamless', 'packshot'], '影棚': ['studio', 'backdrop', 'seamless'],
    '户外': ['outdoor'], '黑白': ['monochrome', 'grayscale', 'black and white'],
    '运动': ['sportswear', 'athletic', 'activewear'], '机能': ['techwear', 'technical'],
    '学院': ['preppy', 'varsity', 'collegiate'],
    '复古': ['vintage', 'retro'], '怀旧': ['vintage', 'retro', 'nostalgic'],
    '全身': ['full-body', 'full-length'], '半身': ['half-body', 'waist-up'],
    '居家': ['at-home', 'indoor', 'interior'], '室内': ['indoor', 'interior', 'room'],
  };
  function expandQueryTerms(q) {
    // Always include the raw query (so exact name / English matches still
    // work), plus any specific English fragments mapped from a Chinese
    // technical keyword present in it.
    const terms = [q];
    for (const cn in SEARCH_SYNONYMS) {
      if (q.includes(cn)) terms.push(...SEARCH_SYNONYMS[cn]);
    }
    return terms;
  }
  function templateSearchBlob(tpl) {
    const spec = tpl.styleSpec || {};
    // Deliberately EXCLUDE the category name ("服饰") and the constant subtitle
    // ("灵感参考") — they're identical on every card, so including them made
    // generic queries match the entire catalog.
    return [
      tpl.name,
      spec.chineseLabel, spec.mood, spec.palette, spec.styling,
      spec.commercialIntent, spec.sceneDirection, spec.shotType,
    ].filter(Boolean).join(' ').toLowerCase();
  }
  function templateMatchesQuery(tpl, q) {
    if (!q) return true;
    const blob = templateSearchBlob(tpl);
    return expandQueryTerms(q).some(term => blob.includes(term));
  }

  function findTemplateById(id) {
    for (const cat of getCategories()) {
      const tpl = getTemplatesForCategory(cat.id).find(t => t.id === id);
      if (tpl) return tpl;
    }
    return null;
  }

  // Search filters the main gallery IN PLACE: typing a keyword keeps the
  // matching style cards on the page so the user can browse and pick one
  // (clicking a card then runs the normal upload → generate flow). There is
  // no dropdown — the filtered gallery is the result.
  function applySearch() {
    const input = $('#search-input');
    if (!input) return;
    const q = input.value.trim().toLowerCase();
    searchQuery = q;
    // Filtering only makes sense on the home gallery — pull the user back to
    // it if they started searching from "我的作品".
    if (q && currentView !== 'home') setView('home');
    renderTemplatesForCategory(currentCategoryId || 'apparel');
  }

  function setupSearch() {
    const toggle = $('#btn-search-toggle');
    const input = $('#search-input');
    if (!toggle || !input) return;

    toggle.addEventListener('click', () => input.focus());

    // Debounce so the masonry isn't rebuilt on every keystroke (esp. when the
    // query shrinks back toward the full 300+ card list).
    let timer = null;
    // The Pinterest feed re-queries on the same box, but each query is a
    // gallery-dl scrape — use a much longer pause (or Enter) before firing.
    let feedTimer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(applySearch, 120);
      clearTimeout(feedTimer);
      feedTimer = setTimeout(() => resetPinFeed(input.value), 800);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        clearTimeout(timer);
        clearTimeout(feedTimer);
        applySearch();
        resetPinFeed(input.value);
      } else if (e.key === 'Escape') {
        clearTimeout(timer);
        clearTimeout(feedTimer);
        input.value = '';
        applySearch();
        resetPinFeed('');
        input.blur();
      }
    });
  }

  // ---- Stock reference search (Pexels) ----
  // Search Pexels, pick a photo → the server downloads it and extracts a
  // styleSpec (the same pipeline curated cards use), returning a style-asset
  // object. We prepend it to the gallery so it behaves exactly like any other
  // style card: upload a product, click the card, generate.
  let stockQuery = '';
  let stockPage = 1;
  let stockHasMore = false;
  let stockLoading = false;
  let stockEnhance = true;   // bias results toward the 日韩清新 house style
  let stockSource = 'pexels'; // active source; overridden by available sources

  // Wire the source switcher from what the server has configured. With 0-1
  // sources the switcher stays hidden; with 2 it appears and defaults to the
  // first (Pinterest ranks ahead of Pexels for this app's aesthetic).
  function applyStockSources(sources) {
    const list = Array.isArray(sources) ? sources : [];
    if (list.length) stockSource = list[0];
    // Home feed prefers Pinterest (denser 日韩清新 results); falls back to
    // Pexels so the feed still works when gallery-dl isn't installed.
    feedSource = list.includes('pinterest') ? 'pinterest' : (list[0] || '');
    const wrap = $('#stock-sources');
    if (!wrap) return;
    wrap.querySelectorAll('button[data-source]').forEach(btn => {
      const on = list.includes(btn.dataset.source);
      btn.hidden = !on;
      btn.classList.toggle('active', btn.dataset.source === stockSource);
    });
    wrap.hidden = list.length < 2;
  }

  // Output is fixed to portrait, so references should compose that way too.
  function stockOrientation() {
    return 'portrait';
  }
  function openStock() {
    show($('#stock-overlay'));
    const input = $('#stock-input');
    if (input) setTimeout(() => input.focus(), 50);
    // First open with an empty grid → show Pexels' curated best right away.
    const grid = $('#stock-grid');
    if (grid && !grid.children.length) runStockSearch(true);
  }
  function closeStock() { hide($('#stock-overlay')); }
  function setStockStatus(msg) {
    const el = $('#stock-status');
    if (!el) return;
    el.textContent = msg || '';
    el.style.display = msg ? '' : 'none';
  }
  async function runStockSearch(reset) {
    if (stockLoading) return;
    const input = $('#stock-input');
    const q = (input ? input.value : '').trim();
    if (reset) { stockQuery = q; stockPage = 1; const g = $('#stock-grid'); if (g) g.innerHTML = ''; }
    stockLoading = true;
    const more = $('#stock-more');
    if (more) more.disabled = true;
    if (reset) setStockStatus(stockQuery ? '搜索中…' : '加载精选中…');
    try {
      const params = new URLSearchParams({
        source: stockSource,
        q: stockQuery,
        page: String(stockPage),
        orientation: stockOrientation(),
        enhance: stockEnhance ? '1' : '0',
      });
      const data = await apiGet(`/api/stock/search?${params.toString()}`);
      const photos = Array.isArray(data.photos) ? data.photos : [];
      stockHasMore = !!data.hasMore;
      if (reset && photos.length === 0) {
        setStockStatus(stockQuery
          ? `没有找到「${stockQuery}」的图片，换个关键词试试（英文关键词结果更多）。`
          : '暂时取不到精选，试试搜索关键词。');
      } else {
        setStockStatus('');
        appendStockTiles(photos);
      }
    } catch (err) {
      setStockStatus(err.message || '搜索失败，请重试');
    } finally {
      stockLoading = false;
      if (more) { more.hidden = !stockHasMore; more.disabled = false; }
    }
  }
  function appendStockTiles(photos) {
    const grid = $('#stock-grid');
    if (!grid) return;
    photos.forEach(p => {
      const tile = document.createElement('div');
      tile.className = 'stock-tile';
      const by = p.photographer ? `<div class="stock-tile-by">📷 ${escapeHtml(p.photographer)}</div>` : '';
      tile.innerHTML = `<img loading="lazy" src="${escapeHtml(p.thumb)}" alt="${escapeHtml(p.alt || '')}" />${by}`;
      tile.addEventListener('click', () => useStockPhoto(p, tile));
      grid.appendChild(tile);
    });
  }
  async function useStockPhoto(photo, tile) {
    if (tile.classList.contains('busy')) return;
    tile.classList.add('busy');
    const spin = document.createElement('div');
    spin.className = 'stock-tile-spin';
    spin.textContent = '分析中…';
    tile.appendChild(spin);
    try {
      const asset = await apiCall('/api/stock/use', { url: photo.full, photographer: photo.photographer || '' });
      // Prepend as a live style card (dedupe on sampleImage, just in case).
      dynamicStyleAssets = [asset, ...dynamicStyleAssets.filter(a => a.sampleImage !== asset.sampleImage)];
      // Clear any active gallery filter so the new card is visible at the top.
      const sInput = $('#search-input');
      if (sInput) sInput.value = '';
      searchQuery = '';
      renderTemplates();
      closeStock();
      toast('已加入风格库，上传产品图后点这张卡片即可生成');
    } catch (err) {
      spin.remove();
      tile.classList.remove('busy');
      toast(err.message || '添加失败，请重试', 'error');
    }
  }
  function setupStock() {
    $('#btn-stock')?.addEventListener('click', openStock);
    $('#stock-close')?.addEventListener('click', closeStock);
    $('#stock-more')?.addEventListener('click', () => { stockPage += 1; runStockSearch(false); });
    const input = $('#stock-input');
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); runStockSearch(true); }
        else if (e.key === 'Escape') closeStock();
      });
    }
    // Quick-keyword chips: proven fashion/editorial queries that surface the
    // high-end references hand-picking used to find.
    $('#stock-chips')?.querySelectorAll('button[data-q]').forEach(chip => {
      chip.addEventListener('click', () => {
        if (input) input.value = chip.dataset.q;
        runStockSearch(true);
      });
    });
    // "清新" toggle: on = bias toward the 日韩清新 look. Re-run current query.
    const enhanceBtn = $('#stock-enhance');
    if (enhanceBtn) {
      enhanceBtn.addEventListener('click', () => {
        stockEnhance = !stockEnhance;
        enhanceBtn.classList.toggle('active', stockEnhance);
        runStockSearch(true);
      });
    }
    // Source switcher (Pinterest / Pexels): switch and re-run the query.
    $('#stock-sources')?.querySelectorAll('button[data-source]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.source === stockSource) return;
        stockSource = btn.dataset.source;
        $('#stock-sources').querySelectorAll('button').forEach(b =>
          b.classList.toggle('active', b.dataset.source === stockSource));
        runStockSearch(true);
      });
    });
    const overlay = $('#stock-overlay');
    if (overlay) overlay.addEventListener('click', (e) => { if (e.target === overlay) closeStock(); });
  }

  // ---- Pinterest home feed ----
  // Pins flow straight into the homepage masonry below the curated style
  // cards — no separate overlay to open. Scrolling near the bottom loads the
  // next page (feed=1: stable pagination, server-cached). The header search
  // box drives BOTH the local card filter and the Pinterest feed query.
  // Pins behave like curated style cards: click → generate (requires a
  // product image, same gate/toast as a card click), heart → favorite into
  // 我的收藏. Either action first runs the import pipeline once
  // (/api/stock/use → download + styleSpec analysis, a few seconds,
  // cached on the photo afterwards).
  let feedSource = '';        // set from auth-status via applyStockSources
  let feedQuery = '';
  let feedPage = 1;
  let feedHasMore = false;
  let feedLoading = false;
  let feedStarted = false;    // first page requested (guards double init)
  let feedSeen = new Set();   // full-res URLs already shown (cross-page dedupe)
  let feedObserver = null;    // sentinel observer (re-armed after each page)

  function feedAvailable() { return !!feedSource; }
  function setFeedStatus(msg, retry) {
    const el = $('#pin-feed-status');
    if (!el) return;
    el.textContent = msg || '';
    el.hidden = !msg;
    el.classList.toggle('retry', !!retry);
  }
  function initPinFeed() {
    if (!feedAvailable() || feedStarted) return;
    feedStarted = true;
    loadPinFeedPage();
  }
  function resetPinFeed(query) {
    const q = (query || '').trim();
    if (!feedAvailable() || !feedStarted) return;
    const grid = $('#template-grid');
    if (q === feedQuery && grid?.querySelector('.pin-card')) return; // same query, keep results
    feedQuery = q;
    feedPage = 1;
    feedHasMore = false;
    feedSeen = new Set();
    // Only clear remote cards; curated cards in this shared masonry stay put.
    grid?.querySelectorAll('.pin-card').forEach(card => card.remove());
    loadPinFeedPage();
  }
  async function loadPinFeedPage() {
    if (feedLoading || !feedAvailable()) return;
    feedLoading = true;
    setFeedStatus(feedPage === 1 ? '正在从 Pinterest 拉取灵感…' : '加载更多…');
    try {
      const params = new URLSearchParams({
        source: feedSource,
        q: feedQuery,
        page: String(feedPage),
        orientation: stockOrientation(),
        enhance: stockEnhance ? '1' : '0',
        feed: '1',
      });
      const data = await apiGet(`/api/stock/search?${params.toString()}`);
      const photos = Array.isArray(data.photos) ? data.photos : [];
      feedHasMore = !!data.hasMore;
      appendPinCards(photos);
      const grid = $('#template-grid');
      if (!grid || !grid.querySelector('.pin-card')) {
        setFeedStatus(feedQuery ? `Pinterest 上没找到「${feedQuery}」，换个关键词试试` : '暂时没有拉到图片，点这里重试', !feedQuery);
      } else {
        setFeedStatus(feedHasMore ? '' : '没有更多了');
      }
    } catch (err) {
      setFeedStatus((err.message || 'Pinterest 加载失败') + '，点这里重试', true);
    } finally {
      feedLoading = false;
      // Re-arm the sentinel observer: observe() re-fires the callback with
      // the CURRENT intersection state, so if the new pins didn't push the
      // sentinel out of range (wide screens, short feed) the next page still
      // loads without requiring a scroll event.
      const sentinel = $('#pin-feed-sentinel');
      if (feedObserver && sentinel) {
        feedObserver.unobserve(sentinel);
        feedObserver.observe(sentinel);
      }
    }
  }
  function appendPinCards(photos) {
    const grid = $('#template-grid');
    if (!grid) return;
    photos.forEach(p => {
      if (!p || !p.full || feedSeen.has(p.full)) return;
      feedSeen.add(p.full);
      const card = document.createElement('div');
      card.className = 'pin-card';
      card.innerHTML = `<img loading="lazy" src="${escapeHtml(p.thumb || p.full)}" alt="${escapeHtml(p.alt || '')}" /><span class="pin-tag">点击生成同款风格</span><button class="template-fav-btn" title="收藏风格" aria-label="收藏风格">${ICON_HEART}</button>`;
      card.querySelector('.template-fav-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        togglePinFavorite(p, card);
      });
      card.addEventListener('click', () => usePinPhoto(p, card));
      grid.appendChild(card);
    });
  }
  // Import a pin once (server downloads it + extracts a styleSpec — the same
  // pipeline curated cards went through at ingest time) and cache the
  // resulting asset on the photo object, so repeat actions (generate again,
  // un/re-favorite) respond instantly like a normal style card.
  async function importPinAsset(photo, card, busyText) {
    if (photo._asset) return photo._asset;
    card.classList.add('busy');
    const spin = document.createElement('div');
    spin.className = 'stock-tile-spin';
    spin.textContent = busyText;
    card.appendChild(spin);
    try {
      const asset = await apiCall('/api/stock/use', { url: photo.full, photographer: photo.photographer || '' });
      photo._asset = asset;
      // Joins the style library silently (dedupe on sampleImage) — no
      // immediate gallery re-render, so the feed doesn't shift under the
      // user's scroll position. The card shows up on the next render/reload.
      dynamicStyleAssets = [asset, ...dynamicStyleAssets.filter(a => a.sampleImage !== asset.sampleImage)];
      return asset;
    } finally {
      spin.remove();
      card.classList.remove('busy');
    }
  }
  async function usePinPhoto(photo, card) {
    if (card.classList.contains('busy')) return;
    // Same gate as a curated style card: generation needs a product image
    // first — no import happens until the click can actually produce a shot.
    if (!refImage) {
      toast('请先点 + 上传产品图', 'error');
      return;
    }
    try {
      const asset = await importPinAsset(photo, card, '分析风格中…');
      startGeneration(dynamicAssetToTemplate(asset, 0));
    } catch (err) {
      toast(err.message || '风格分析失败，请重试', 'error');
    }
  }
  async function togglePinFavorite(photo, card) {
    if (card.classList.contains('busy')) return;
    try {
      const asset = await importPinAsset(photo, card, '收藏中…');
      toggleFavoriteTemplate(dynamicAssetToTemplate(asset, 0));
      syncPinFavButton(photo, card);
    } catch (err) {
      toast(err.message || '收藏失败，请重试', 'error');
    }
  }
  function syncPinFavButton(photo, card) {
    const btn = card.querySelector('.template-fav-btn');
    if (!btn) return;
    const active = !!(photo._asset && isTemplateFavorited(photo._asset.id));
    btn.classList.toggle('active', active);
    const title = active ? '取消收藏' : '收藏风格';
    btn.title = title;
    btn.setAttribute('aria-label', title);
  }
  function setupPinFeed() {
    // Infinite scroll: fetch the next page when the sentinel under the feed
    // comes within ~600px of the viewport. The sentinel lives inside
    // #view-home, so it never fires while "我的作品" is shown.
    const sentinel = $('#pin-feed-sentinel');
    if (sentinel && 'IntersectionObserver' in window) {
      feedObserver = new IntersectionObserver((entries) => {
        if (!entries.some(e => e.isIntersecting)) return;
        if (!feedStarted || feedLoading || !feedHasMore) return;
        feedPage += 1;
        loadPinFeedPage();
      }, { rootMargin: '600px 0px' });
      feedObserver.observe(sentinel);
    }
    // Error status doubles as a retry button.
    $('#pin-feed-status')?.addEventListener('click', () => {
      const el = $('#pin-feed-status');
      if (el && el.classList.contains('retry') && !feedLoading) loadPinFeedPage();
    });
  }

  // ---- Lightbox ----
  // itemId: when opened from a finished gallery card, shows the action bar
  // (download / 换一张) — on touch devices the lightbox is the primary place
  // these actions are reachable.
  let _lightboxItemId = null;
  function openLightbox(src, itemId) {
    const img = $('#lightbox-img');
    if (img) img.src = src;
    _lightboxItemId = itemId || null;
    const item = _lightboxItemId ? myItems.find(x => x.id === _lightboxItemId) : null;
    const actions = $('#lightbox-actions');
    if (actions) actions.hidden = !(item && item.status === 'done');
    show($('#lightbox'));
  }
  function closeLightbox() {
    _lightboxItemId = null;
    hide($('#lightbox'));
  }

  // ---- Auth ----
  function showLoginOverlay(errMsg) {
    const overlay = $('#login-overlay');
    if (!overlay) return;
    show(overlay);
    const errEl = $('#login-error');
    if (errEl) errEl.textContent = errMsg || '';
    setTimeout(() => $('#login-username')?.focus(), 50);
  }
  function hideLoginOverlay() { hide($('#login-overlay')); }

  // ---- Accounts mode: register/login toggle + credit display ----
  function setAuthMode(mode) {
    authMode = mode === 'register' ? 'register' : 'login';
    const btn = $('#login-btn');
    const sub = $('#login-subtitle');
    const switchText = $('#login-switch-text');
    const switchLink = $('#login-switch-link');
    if (authMode === 'register') {
      if (btn) btn.textContent = '注册';
      if (sub) sub.textContent = '创建你的账号';
      if (switchText) switchText.textContent = '已有账号？';
      if (switchLink) switchLink.textContent = '去登录';
    } else {
      if (btn) btn.textContent = '登录';
      if (sub) sub.textContent = '登录你的账号';
      if (switchText) switchText.textContent = '还没有账号？';
      if (switchLink) switchLink.textContent = '注册';
    }
    syncTurnstileWidget();
  }

  function ensureTurnstileScript() {
    if (window.turnstile) return Promise.resolve(window.turnstile);
    if (turnstileLoadPromise) return turnstileLoadPromise;
    turnstileLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      script.async = true;
      script.defer = true;
      script.onload = () => resolve(window.turnstile);
      script.onerror = () => reject(new Error('Turnstile unavailable'));
      document.head.appendChild(script);
    });
    return turnstileLoadPromise;
  }

  async function syncTurnstileWidget() {
    const wrap = $('#turnstile-wrap');
    if (!wrap) return;
    const needed = accountsMode && authMode === 'register' && !!turnstileSiteKey;
    wrap.hidden = !needed;
    if (!needed) return;
    try {
      const turnstile = await ensureTurnstileScript();
      if (!turnstile) throw new Error('Turnstile unavailable');
      if (turnstileWidgetId === null) {
        turnstileWidgetId = turnstile.render('#turnstile-widget', {
          sitekey: turnstileSiteKey,
          theme: 'dark',
          language: 'zh-cn',
        });
      } else {
        turnstile.reset(turnstileWidgetId);
      }
    } catch (e) {
      const errEl = $('#login-error');
      if (errEl) errEl.textContent = '人机验证加载失败，请刷新后重试';
    }
  }

  function resetTurnstileWidget() {
    if (window.turnstile && turnstileWidgetId !== null) {
      window.turnstile.reset(turnstileWidgetId);
    }
  }
  // Configure the login overlay fields for the active server mode.
  function applyAuthModeUI() {
    const emailEl = $('#login-email');
    const userEl = $('#login-username');
    const switchEl = $('#login-switch');
    if (accountsMode) {
      if (userEl) userEl.hidden = true;
      if (emailEl) emailEl.hidden = false;
      if (switchEl) switchEl.hidden = !registrationEnabled;
      setAuthMode(registrationEnabled ? authMode : 'login');
    } else {
      if (userEl) userEl.hidden = false;
      if (emailEl) emailEl.hidden = true;
      if (switchEl) switchEl.hidden = true;
      const btn = $('#login-btn'); if (btn) btn.textContent = '进入';
      const sub = $('#login-subtitle'); if (sub) sub.textContent = '请输入访问凭据';
    }
  }
  function setCreditDisplay(credits, admin) {
    const el = $('#credit-amount');
    if (el) el.textContent = (admin || credits < 0) ? '∞' : String(credits);
  }
  function updateAccountUI(data) {
    const chip = $('#account-chip');
    if (!chip) return;
    if (!accountsMode || !authToken) { chip.classList.add('hidden'); return; }
    chip.classList.remove('hidden');
    if (data && typeof data.credits === 'number') setCreditDisplay(data.credits, data.admin);
  }
  async function refreshAccount() {
    if (!accountsMode || !authToken) return;
    try {
      const data = await apiGet('/api/account');
      if (data && data.enabled) updateAccountUI(data);
    } catch (e) { /* token refresh handled by apiGet on 401 */ }
  }
  function logout() {
    localStorage.removeItem('aid_token');
    authToken = '';
    $('#account-chip')?.classList.add('hidden');
    showLoginOverlay();
  }
  // Recharge UI lands with the payment integration (step 4); placeholder for now.
  function openRecharge() {
    toast('充值功能即将开放，敬请期待', 'error');
  }

  async function submitLogin() {
    const pwd = $('#login-password').value;
    const errEl = $('#login-error');
    const btn = $('#login-btn');
    let url = '/api/login';
    let payload;
    if (accountsMode) {
      const email = ($('#login-email').value || '').trim();
      if (!email || !pwd) { errEl.textContent = '请输入邮箱和密码'; return; }
      if (authMode === 'register' && pwd.length < 6) { errEl.textContent = '密码至少 6 位'; return; }
      url = authMode === 'register' ? '/api/register' : '/api/login';
      payload = { email, password: pwd };
      if (authMode === 'register' && turnstileSiteKey) {
        const turnstileToken = (window.turnstile && turnstileWidgetId !== null)
          ? window.turnstile.getResponse(turnstileWidgetId)
          : '';
        if (!turnstileToken) { errEl.textContent = '请完成人机验证'; return; }
        payload.turnstile_token = turnstileToken;
      }
    } else {
      const user = $('#login-username').value.trim();
      if (!user || !pwd) { errEl.textContent = '请输入用户名和密码'; return; }
      payload = { username: user, password: pwd };
    }
    btn.disabled = true;
    errEl.textContent = '';
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { errEl.textContent = data.error || '操作失败'; return; }
      authToken = data.token || '';
      localStorage.setItem('aid_token', authToken);
      $('#login-password').value = '';
      hideLoginOverlay();
      if (accountsMode) updateAccountUI(data);
      await loadDynamicStyleAssets();
      initPinFeed();
      await loadFavoriteCounts(allTemplateIds());
      renderTemplatesForCategory(currentCategoryId);
      renderFavoriteGrid();
      startTour();
    } catch (err) {
      errEl.textContent = '网络错误，请重试';
    } finally {
      btn.disabled = false;
      if (authMode === 'register' && turnstileSiteKey) resetTurnstileWidget();
    }
  }
  async function initAuthGate() {
    let required = true;
    try {
      const resp = await fetch('/api/auth-status');
      const data = await resp.json();
      required = !!data.required;
      accountsMode = !!data.accounts;
      registrationEnabled = !!data.registrationEnabled;
      turnstileSiteKey = data.turnstileSiteKey || '';
      // #btn-stock (the separate reference-search overlay) stays hidden —
      // Pinterest now feeds the homepage gallery directly. The overlay code
      // path is kept dormant like the mode-switcher; re-enable by restoring
      // `stockBtn.hidden = !data.stock` here.
      applyStockSources(data.stockSources);
    } catch (err) { required = true; }
    applyAuthModeUI();
    if (!required) { hideLoginOverlay(); return true; }
    if (!authToken) { showLoginOverlay(); return false; }
    // We have a token — signed and stateless, so it survives server restarts.
    // It can still be invalid: 7-day expiry, or a password/SESSION_SECRET
    // change. Verify via a tiny auth-required endpoint; treat 401 as expired.
    // (Pings /api/verify, NOT /api/style-assets — the latter is the 457KB
    // gallery payload and was being fetched twice serially on every cold load.)
    try {
      const ping = await fetch('/api/verify', {
        headers: { 'Authorization': 'Bearer ' + authToken }
      });
      if (ping.status === 401) {
        localStorage.removeItem('aid_token');
        authToken = '';
        showLoginOverlay('登录已失效，请重新登录');
        return false;
      }
    } catch (err) { /* network issue — don't force re-login on offline */ }
    if (accountsMode) refreshAccount();
    hideLoginOverlay();
    return true;
  }

  // ---- Events ----
  const fi = $('#file-input');
  if (fi) fi.addEventListener('change', () => { if (fi.files[0]) handleUpload(fi.files[0]); });

  $('#dock-home')?.addEventListener('click', () => setView('home'));
  $('#dock-upload')?.addEventListener('click', () => fi?.click());
  $('#dock-my')?.addEventListener('click', () => setView('my'));
  document.querySelectorAll('.my-tab').forEach(tab => {
    tab.addEventListener('click', () => setMyTab(tab.dataset.myTab));
  });

  // Drag-anywhere as a drop zone
  document.addEventListener('dragover', e => { e.preventDefault(); });
  document.addEventListener('drop', e => {
    e.preventDefault();
    const f = e.dataTransfer?.files?.[0];
    if (f && f.type.startsWith('image/')) handleUpload(f);
  });

  $('#lightbox')?.addEventListener('click', (e) => {
    // Clicks on the action bar must not dismiss the lightbox.
    if (e.target.closest('.lightbox-actions')) return;
    closeLightbox();
  });
  $('#lightbox-download')?.addEventListener('click', () => {
    if (_lightboxItemId) downloadItem(_lightboxItemId);
  });
  $('#lightbox-regen')?.addEventListener('click', () => {
    const id = _lightboxItemId;
    closeLightbox();
    if (id) regenerateItem(id);
  });
  $('#lightbox-close')?.addEventListener('click', closeLightbox);
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!$('#lightbox').classList.contains('hidden')) closeLightbox();
  });

  $('#login-btn')?.addEventListener('click', submitLogin);
  $('#login-password')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); submitLogin(); }
  });
  $('#login-username')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); $('#login-password').focus(); }
  });
  $('#login-email')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); $('#login-password').focus(); }
  });
  $('#login-switch-link')?.addEventListener('click', e => {
    e.preventDefault();
    if (!registrationEnabled) return;
    setAuthMode(authMode === 'login' ? 'register' : 'login');
    $('#login-error').textContent = '';
  });
  $('#logout-btn')?.addEventListener('click', logout);
  $('#recharge-btn')?.addEventListener('click', openRecharge);

  function allTemplateIds() {
    const ids = [];
    for (const cat of getCategories()) {
      for (const t of getTemplatesForCategory(cat.id)) ids.push(t.id);
    }
    return ids;
  }

  // ---- Init ----
  document.addEventListener('DOMContentLoaded', async () => {
    const authed = await initAuthGate();
    favoriteTemplates = loadFavoriteTemplates();
    myItems = await loadMyItems();
    renderTemplates();
    updateDockUploadButton();
    setupSearch();
    setupStock();
    setupPinFeed();
    // Mode switcher (gallery <-> quick)
    document.querySelectorAll('.mode-chip[data-top-mode]').forEach(btn => {
      btn.addEventListener('click', () => setTopMode(btn.dataset.topMode));
    });
    // Quick panel: count grid (1-6)
    document.querySelectorAll('#quick-count-grid button').forEach(btn => {
      btn.addEventListener('click', () => {
        quickCount = parseInt(btn.dataset.count, 10) || 6;
        localStorage.setItem('aid_quick_count_v1', String(quickCount));
        document.querySelectorAll('#quick-count-grid button').forEach(b => {
          b.classList.toggle('active', parseInt(b.dataset.count, 10) === quickCount);
        });
        refreshQuickPanelState();
      });
    });
    document.querySelectorAll('#quick-count-grid button').forEach(b => {
      b.classList.toggle('active', parseInt(b.dataset.count, 10) === quickCount);
    });
    // Quick panel: self-contained upload (no dock dependency)
    $('#quick-file-input')?.addEventListener('change', (e) => {
      const f = e.target.files?.[0];
      if (f) handleUpload(f);
    });
    $('#quick-upload-clear')?.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      refImage = null;
      updateDockUploadButton();
      updateOnboardingHint();
      refreshQuickPanelState();
    });
    $('#quick-generate-btn')?.addEventListener('click', submitQuickGenerate);
    applyTopMode();  // initial gallery/quick state
    renderMySection();
    updateDockBadges();
    // Resume polling for jobs that were in flight before the last reload.
    ensureJobPolling();
    // Mobile browsers throttle/freeze timers in background tabs; poll
    // immediately when the user comes back so results show up right away.
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        ensureJobPolling();
        pollPendingJobs(true);
      }
    });
    if (authed) {
      await loadDynamicStyleAssets();
      initPinFeed();
      // Fetch favorite counts in the background, re-render once available.
      loadFavoriteCounts(allTemplateIds()).then(() => {
        renderTemplatesForCategory(currentCategoryId);
        renderFavoriteGrid();
      });
      startTour();
    }
  });
})();
