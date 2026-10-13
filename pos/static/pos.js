/* MultiPOS till screen. Prices are integer cents everywhere. */
(() => {
  const C = window.POS, F = C.features;
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name=csrf-token]').content;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = (c) => {
    c = Math.round(c || 0); const neg = c < 0; c = Math.abs(c);
    return (neg ? '-' : '') + Math.floor(c / 100).toLocaleString('en-KE') + '.' + String(c % 100).padStart(2, '0');
  };
  const toCents = (v) => { const n = parseFloat(String(v ?? '').replace(/,/g, '')); return isNaN(n) ? 0 : Math.round(n * 100); };

  let catalog = { categories: [], products: [], tables: [], attendants: [] };
  let byId = {};
  let state;            // current order
  let activeCat = 'all';
  let pollTimer = null;

  function blank() {
    return { saleId: null, receiptNo: '', tableId: null, items: [], discount: 0, dirty: false };
  }

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (r.status === 401) { location.href = '/login?next=/pos'; throw new Error('Logged out'); }
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  }

  function toast(msg, bad) {
    const t = $('toast'); t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.style.display = 'block';
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.style.display = 'none'), bad ? 5000 : 2500);
  }

  // ---------- catalogue ----------
  async function loadCatalog() {
    catalog = await api('GET', '/api/catalog');
    byId = Object.fromEntries(catalog.products.map((p) => [p.id, p]));
    renderCats(); renderProducts(); refreshOpenCount();
  }

  function renderCats() {
    const used = new Set(catalog.products.map((p) => p.category_id));
    const cats = [{ id: 'all', name: 'All' }, ...catalog.categories.filter((c) => used.has(c.id))];
    $('cats').innerHTML = cats.map((c) => `<button data-id="${c.id}" class="${String(c.id) === String(activeCat) ? 'on' : ''}">${esc(c.name)}</button>`).join('');
  }

  function filtered() {
    const q = $('search').value.trim().toLowerCase();
    return catalog.products.filter((p) =>
      (activeCat === 'all' || String(p.category_id) === String(activeCat)) &&
      (!q || p.name.toLowerCase().includes(q) || (p.sku || '').toLowerCase().includes(q) || (p.barcode || '') === q));
  }

  function renderProducts() {
    const list = filtered();
    $('products').innerHTML = list.length ? list.map((p) => {
      const stock = F.track_stock && p.track_stock && !p.is_service;
      const out = stock && p.stock_qty <= 0;
      return `<button class="prod ${out ? 'out' : ''}" data-id="${p.id}">
        <span class="n">${esc(p.name)}</span>
        ${F.prescriptions && p.requires_prescription ? '<span class="rx">Rx only</span>' : ''}
        <span><span class="p">${money(p.price)}</span>${p.unit !== 'pc' ? `<span class="s"> /${esc(p.unit)}</span>` : ''}
        ${stock ? `<div class="s">${+p.stock_qty.toFixed(3)} in stock</div>` : ''}</span></button>`;
    }).join('') : '<div class="empty">No items match.</div>';
  }

  // ---------- cart ----------
  function addProduct(id) {
    const p = byId[id]; if (!p) return;
    // Tapping the same item again adds to its line, unless that line has a note or needs its own attendant.
    const existing = !(F.attendants && p.is_service) && state.items.find((i) => i.product_id === p.id && !i.note && !(i.sent > 0));
    if (existing) existing.qty = +(existing.qty + 1).toFixed(3);
    else state.items.push({ product_id: p.id, qty: 1, note: '', attendant_id: '', item_id: null, sent: 0 });
    state.dirty = true; renderCart();
    $('cart').scrollTop = $('cart').scrollHeight;
  }

  function lineGross(i) { return Math.round(byId[i.product_id].price * i.qty); }

  function calcTotals() {
    const gross = state.items.reduce((s, i) => s + (byId[i.product_id] ? lineGross(i) : 0), 0);
    const discount = Math.min(state.discount, gross);
    let tax = 0;
    state.items.forEach((i) => {
      const p = byId[i.product_id]; if (!p || !gross) return;
      const share = lineGross(i) - discount * lineGross(i) / gross;
      tax += C.inclusive ? share * p.tax_rate / (100 + p.tax_rate) : share * p.tax_rate / 100;
    });
    const service = Math.round((gross - discount) * C.serviceRate / 100);
    const total = (C.inclusive ? gross - discount : gross - discount + Math.round(tax)) + service;
    return { gross, discount, service, tax: Math.round(tax), total };
  }

  function renderCart() {
    const cart = $('cart');
    if (!state.items.length) {
      cart.innerHTML = '<div class="empty">Tap items to add them to the order.</div>';
    } else {
      cart.innerHTML = state.items.map((i, idx) => {
        const p = byId[i.product_id];
        if (!p) return `<div class="line"><div class="t"><span>Removed item</span></div></div>`;
        const step = F.decimal_qty && p.allow_decimal ? '0.001' : '1';
        const atts = F.attendants && p.is_service
          ? `<select data-att="${idx}"><option value="">Attendant…</option>${catalog.attendants.map((a) =>
              `<option value="${a.id}" ${String(a.id) === String(i.attendant_id) ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}</select>` : '';
        const note = F.item_notes ? `<input class="lnote" data-note="${idx}" placeholder="Note e.g. no onions" value="${esc(i.note)}">` : '';
        const sent = F.kitchen_tickets && i.sent > 0 ? `<span class="sent">✓ ${+i.sent} sent to kitchen</span>` : '';
        return `<div class="line">
          <div class="t"><span>${esc(p.name)}</span><span>${money(lineGross(i))}</span></div>
          <div class="c">
            <button data-dec="${idx}" aria-label="less">−</button>
            <input class="q" data-qty="${idx}" type="number" min="0" step="${step}" value="${+i.qty}">
            <button data-inc="${idx}" aria-label="more">+</button>
            <span class="muted small">× ${money(p.price)}</span>
            <button class="x" data-del="${idx}" aria-label="remove">✕</button>
          </div>
          ${note || atts ? `<div class="c">${atts}${note}</div>` : ''}${sent}</div>`;
      }).join('');
    }
    const t = calcTotals();
    $('tGross').textContent = money(t.gross);
    $('tDisc').textContent = t.discount ? '-' + money(t.discount) : '0.00';
    if ($('tService')) $('tService').textContent = money(t.service);
    $('tTax').textContent = money(t.tax);
    $('tTotal').textContent = money(t.total);
    $('orderNo').textContent = state.receiptNo ? `Order ${state.receiptNo}` : 'New order';
    if ($('tableName')) {
      const tb = catalog.tables.find((x) => x.id === state.tableId);
      $('tableName').textContent = tb ? tb.name : 'none';
    }
    if ($('rxRef')) {
      const needRx = state.items.some((i) => byId[i.product_id]?.requires_prescription);
      $('rxRef').style.display = needRx ? '' : 'none';
    }
  }

  function payload() {
    return {
      items: state.items.map((i) => ({ product_id: i.product_id, qty: i.qty, note: i.note, attendant_id: i.attendant_id || null, item_id: i.item_id })),
      discount: (state.discount / 100).toFixed(2),
      table_id: state.tableId,
      order_label: $('orderLabel')?.value || '',
      customer_name: $('custName')?.value || '',
      customer_phone: $('custPhone')?.value || '',
      prescription_ref: $('rxRef')?.value || '',
    };
  }

  function loadOrder(o) {
    state = blank();
    state.saleId = o.id; state.receiptNo = o.receipt_no; state.tableId = o.table_id; state.discount = o.discount;
    state.items = o.items.map((i) => ({ product_id: i.product_id, qty: i.qty, note: i.note || '', attendant_id: i.attendant_id || '', item_id: i.id, sent: i.kitchen_sent }));
    if ($('orderLabel')) $('orderLabel').value = o.order_label || '';
    if ($('custName')) $('custName').value = o.customer_name || '';
    if ($('custPhone')) $('custPhone').value = o.customer_phone || '';
    if ($('rxRef')) $('rxRef').value = o.prescription_ref || '';
    renderCart();
    return o;
  }

  async function save() {
    if (!state.items.length) throw new Error('Add items first');
    if (state.saleId && !state.dirty) return loadOrder(await api('GET', `/api/orders/${state.saleId}`));
    const o = state.saleId ? await api('PUT', `/api/orders/${state.saleId}`, payload())
                           : await api('POST', '/api/orders', payload());
    loadOrder(o);
    return o;
  }

  function resetOrder() {
    state = blank();
    ['orderLabel', 'custName', 'custPhone', 'rxRef'].forEach((id) => { if ($(id)) $(id).value = ''; });
    renderCart(); $('search').focus();
  }

  // ---------- printing ----------
  function browserPrint(url) { $('printFrame').src = url; }

  async function printReceipt(id) {
    try {
      const r = await api('POST', `/api/orders/${id}/print`);
      if (r.browser) browserPrint(`/receipt/${id}?print=1`); else toast('Receipt sent to printer');
    } catch (e) { toast(e.message, true); }
  }

  // ---------- payment ----------
  let current = null;   // order json from server
  let method = 'cash';

  function showPane(which) {
    $('payForm').hidden = which !== 'form'; $('payWaiting').hidden = which !== 'wait'; $('payDone').hidden = which !== 'done';
  }

  function setMethod(m) {
    method = m;
    document.querySelectorAll('.paytabs button').forEach((b) => b.classList.toggle('on', b.dataset.m === m));
    document.querySelectorAll('[data-pane]').forEach((p) => (p.hidden = p.dataset.pane !== m));
    const focus = { cash: 'cashIn', mpesa: 'mpPhone', code: 'mcCode' }[m];
    setTimeout(() => $(focus).focus(), 30);
  }

  function fillPay(o) {
    current = o;
    $('payReceipt').textContent = o.receipt_no;
    $('payDue').textContent = `${C.currency} ${money(o.balance)}`;
    $('cashIn').value = ''; $('changeOut').textContent = '';
    $('mpAmount').value = Math.ceil(o.balance / 100);
    $('mcAmount').value = (o.balance / 100).toFixed(2);
    if (!$('mpPhone').value) $('mpPhone').value = o.customer_phone || $('custPhone')?.value || '';
    const b = o.balance / 100, opts = [b, Math.ceil(b / 100) * 100, Math.ceil(b / 500) * 500, Math.ceil(b / 1000) * 1000];
    $('quickCash').innerHTML = [...new Set(opts.map((x) => +x.toFixed(2)))].map((x, k) =>
      `<button data-cash="${x}">${k === 0 ? 'Exact' : x.toLocaleString('en-KE')}</button>`).join('');
    const done = o.payments.filter((p) => p.status === 'completed');
    $('paidSoFar').innerHTML = done.length ? 'Already paid: ' + done.map((p) =>
      `${p.method === 'cash' ? 'Cash' : 'M-Pesa'} ${money(p.amount)}${p.reference ? ' (' + esc(p.reference) + ')' : ''}`).join(', ') : '';
  }

  function finish(o) {
    current = o; showPane('done');
    $('doneChange').textContent = o.change_due ? `Change: ${C.currency} ${money(o.change_due)}` : '';
    if (C.autoPrint) printReceipt(o.id);
    loadCatalog();  // refresh stock & tables
  }

  function after(o) {
    if (o.status === 'completed') return finish(o);
    loadOrder(o); fillPay(o); showPane('form');
    toast(`Part payment recorded. ${money(o.balance)} still due.`);
  }

  async function openPay() {
    try {
      const o = await save();
      if (o.status === 'completed') return toast('This order is already paid');
      fillPay(o); showPane('form'); setMethod('cash');
      $('payModal').classList.add('show');
      if (o.status === 'pending_payment') { showPane('wait'); startPolling(); }
    } catch (e) { toast(e.message, true); }
  }

  function startPolling() {
    stopPolling();
    const started = Date.now();
    const tick = async () => {
      try {
        const o = await api('GET', `/api/orders/${current.id}/status`);
        const last = o.payments.filter((p) => p.method === 'mpesa').slice(-1)[0];
        if (o.status === 'completed') { stopPolling(); return finish(o); }
        if (last && last.status === 'completed') { stopPolling(); return after(o); }
        if (last && ['failed', 'cancelled'].includes(last.status)) {
          stopPolling(); loadOrder(o); fillPay(o); showPane('form'); setMethod('mpesa');
          return toast(`M-Pesa: ${last.result_desc || last.status}`, true);
        }
        $('waitMsg').textContent = `Waiting ${Math.round((Date.now() - started) / 1000)}s…`;
      } catch (e) { $('waitMsg').textContent = e.message; }
      pollTimer = setTimeout(tick, 2500);
    };
    pollTimer = setTimeout(tick, 2000);
  }
  function stopPolling() { clearTimeout(pollTimer); pollTimer = null; }

  // ---------- events ----------
  $('cats').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    activeCat = b.dataset.id; renderCats(); renderProducts();
  });
  $('products').addEventListener('click', (e) => { const b = e.target.closest('.prod'); if (b) addProduct(+b.dataset.id); });
  $('search').addEventListener('input', renderProducts);
  $('search').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const q = e.target.value.trim();
    const hit = catalog.products.find((p) => p.barcode && p.barcode === q) || (filtered().length === 1 ? filtered()[0] : null);
    if (hit) { addProduct(hit.id); e.target.value = ''; renderProducts(); }
    else if (q) toast('No item with that barcode/name', true);
  });

  $('cart').addEventListener('click', (e) => {
    const t = e.target, d = t.dataset;
    if (d.inc !== undefined) state.items[d.inc].qty = +(state.items[d.inc].qty + 1).toFixed(3);
    else if (d.dec !== undefined) {
      const i = state.items[d.dec]; i.qty = +(i.qty - 1).toFixed(3);
      if (i.qty <= 0) state.items.splice(d.dec, 1);
    } else if (d.del !== undefined) state.items.splice(d.del, 1);
    else return;
    state.dirty = true; renderCart();
  });
  $('cart').addEventListener('change', (e) => {
    const d = e.target.dataset;
    if (d.qty !== undefined) {
      const v = parseFloat(e.target.value);
      if (!(v > 0)) state.items.splice(d.qty, 1); else state.items[d.qty].qty = v;
      renderCart();
    } else if (d.note !== undefined) state.items[d.note].note = e.target.value;
    else if (d.att !== undefined) state.items[d.att].attendant_id = e.target.value;
    state.dirty = true;
  });

  $('btnDiscount').addEventListener('click', (e) => {
    e.preventDefault();
    const v = prompt(`Discount: enter an amount (e.g. 50) or a percentage (e.g. 10%)${C.isManager ? '' : '\nCashiers: max 10%'}`, '');
    if (v === null) return;
    const gross = calcTotals().gross;
    state.discount = v.trim().endsWith('%') ? Math.round(gross * parseFloat(v) / 100) || 0 : toCents(v);
    state.dirty = true; renderCart();
  });

  $('btnNew').addEventListener('click', () => {
    if (state.items.length && !state.saleId && !confirm('Clear this order?')) return;
    resetOrder();
  });
  $('btnBill').addEventListener('click', async () => {
    try { const o = await save(); browserPrint(`/receipt/${o.id}?print=1`); } catch (e) { toast(e.message, true); }
  });
  $('btnPay').addEventListener('click', openPay);

  if ($('btnHold')) $('btnHold').addEventListener('click', async () => {
    try { const o = await save(); toast(`Order ${o.receipt_no} saved`); resetOrder(); refreshOpenCount(); loadCatalog(); }
    catch (e) { toast(e.message, true); }
  });
  if ($('btnKitchen')) $('btnKitchen').addEventListener('click', async () => {
    try {
      const o = await save();
      const r = await api('POST', `/api/orders/${o.id}/kitchen`);
      if (!r.printed) return toast('Nothing new to send');
      if (r.browser) browserPrint(`/kitchen/${o.id}?print=1`);
      toast(`${r.printed} item(s) sent to kitchen`);
      setTimeout(async () => loadOrder(await api('GET', `/api/orders/${o.id}`)), 800);
      refreshOpenCount();
    } catch (e) { toast(e.message, true); }
  });

  // tables
  if ($('btnTable')) $('btnTable').addEventListener('click', async () => {
    await loadCatalog();
    $('tableGrid').innerHTML = catalog.tables.map((t) =>
      `<button data-id="${t.id}" data-order="${t.order_id || ''}" class="${t.order_id ? 'busy' : ''}">${esc(t.name)}<br><span class="small muted">${t.order_id ? 'occupied' : t.seats + ' seats'}</span></button>`).join('')
      || '<p class="muted">No tables yet. Add them under ' + 'Menu → Tables.</p>';
    $('tableModal').classList.add('show');
  });
  $('tableGrid').addEventListener('click', async (e) => {
    const b = e.target.closest('button'); if (!b) return;
    $('tableModal').classList.remove('show');
    if (b.dataset.order) {
      if (state.items.length && !state.saleId && !confirm('Discard the current unsaved order?')) return;
      loadOrder(await api('GET', `/api/orders/${b.dataset.order}`));
    } else { state.tableId = +b.dataset.id; state.dirty = true; renderCart(); }
  });

  // open orders
  async function refreshOpenCount() {
    if (!$('openCount')) return;
    try { const r = await api('GET', '/api/orders'); $('openCount').textContent = r.orders.length; return r.orders; } catch (e) { return []; }
  }
  if ($('btnOrders')) $('btnOrders').addEventListener('click', async () => {
    const orders = await refreshOpenCount();
    $('orderList').innerHTML = orders.length ? orders.map((o) => `<div class="o" data-id="${o.id}">
      <div><b>${esc(o.table_name || o.order_label || o.customer_name || o.receipt_no)}</b>
      <div class="small muted">${esc(o.receipt_no)} · ${esc(o.created_at.slice(11, 16))} · ${esc(o.cashier)} ${o.status === 'pending_payment' ? '· <span class="pill pending_payment">M-Pesa waiting</span>' : ''}</div></div>
      <div class="num"><b>${money(o.total)}</b>${o.paid ? `<div class="small muted">paid ${money(o.paid)}</div>` : ''}
      <div><button class="btn ghost sm" data-cancel="${o.id}">Cancel</button></div></div></div>`).join('')
      : '<p class="muted">No open orders.</p>';
    $('ordersModal').classList.add('show');
  });
  $('orderList').addEventListener('click', async (e) => {
    const cancel = e.target.closest('[data-cancel]');
    if (cancel) {
      e.stopPropagation();
      const reason = prompt('Reason for cancelling this order?'); if (!reason) return;
      try { await api('POST', `/api/orders/${cancel.dataset.cancel}/cancel`, { reason }); toast('Order cancelled');
        if (state.saleId === +cancel.dataset.cancel) resetOrder();
        $('btnOrders').click(); loadCatalog(); } catch (err) { toast(err.message, true); }
      return;
    }
    const row = e.target.closest('.o'); if (!row) return;
    $('ordersModal').classList.remove('show');
    loadOrder(await api('GET', `/api/orders/${row.dataset.id}`));
  });

  $('extraFields').addEventListener('input', () => (state.dirty = true));

  document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => b.closest('.modal-bg').classList.remove('show')));

  // payment modal events
  document.querySelector('.paytabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setMethod(b.dataset.m); });
  $('quickCash').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return; $('cashIn').value = b.dataset.cash; $('cashIn').dispatchEvent(new Event('input'));
  });
  $('cashIn').addEventListener('input', () => {
    const ch = toCents($('cashIn').value) - current.balance;
    $('changeOut').textContent = ch >= 0 ? `Change: ${C.currency} ${money(ch)}` : `Short by ${money(-ch)} (will be a part payment)`;
  });
  $('cashIn').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('doCash').click(); });

  async function busy(btn, fn) { btn.disabled = true; try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; } }

  $('doCash').addEventListener('click', (e) => busy(e.target, async () => {
    after(await api('POST', `/api/orders/${current.id}/pay/cash`, { tendered: $('cashIn').value }));
  }));
  $('doCode').addEventListener('click', (e) => busy(e.target, async () => {
    after(await api('POST', `/api/orders/${current.id}/pay/mpesa-code`, { code: $('mcCode').value, amount: $('mcAmount').value }));
    $('mcCode').value = '';
  }));
  $('doMpesa').addEventListener('click', (e) => busy(e.target, async () => {
    const o = await api('POST', `/api/orders/${current.id}/pay/mpesa`, { phone: $('mpPhone').value, amount: $('mpAmount').value });
    current = o; $('waitMsg').textContent = o.message || ''; showPane('wait'); startPolling();
  }));
  $('cancelMpesa').addEventListener('click', async () => {
    stopPolling();
    try { const o = await api('POST', `/api/orders/${current.id}/cancel-mpesa`); loadOrder(o); fillPay(o); } catch (e) { toast(e.message, true); }
    showPane('form');
  });
  $('donePrint').addEventListener('click', () => printReceipt(current.id));
  $('doneNew').addEventListener('click', () => { $('payModal').classList.remove('show'); resetOrder(); refreshOpenCount(); });
  $('payClose').addEventListener('click', () => {
    stopPolling(); $('payModal').classList.remove('show');
    if (current && current.status === 'completed') resetOrder();
    refreshOpenCount();
  });

  window.addEventListener('beforeunload', (e) => { if (state.dirty && state.items.length && !state.saleId) { e.preventDefault(); e.returnValue = ''; } });

  state = blank();
  renderCart();
  loadCatalog().catch((e) => toast(e.message, true));
})();
