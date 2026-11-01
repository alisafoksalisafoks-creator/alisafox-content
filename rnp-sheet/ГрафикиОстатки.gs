/**
 * РНП AlisaFox — кнопки «График товара» и «Остатки».
 *
 * Что делает:
 *  - «📈 График товара»: окно с графиком по артикулу за период: заказы, цена, ДРР, реклама ₽,
 *    показы/клики, место в выдаче, прибыль на 1 шт., остаток FBO, акции и автоматические события
 *    («что двигало заказы»). Данные — из листов «РНП» и «API Общий ДРР» этой таблицы.
 *  - «📦 Остатки»: остатки FBO по кластерам по дням + FBS, текущий запас в днях по кластерам.
 *    Сейчас — живой запрос в Ozon Seller API, история — из листа «API Остатки кластеры»,
 *    который пополняется раз в день функцией rnpSnapshot (ставится кнопкой меню один раз).
 *
 * Ключи Ozon берутся из «Свойств скрипта» (Настройки проекта → Свойства скрипта).
 * Если их там нет под привычными именами — меню «Ключи Ozon для остатков» попросит ввести.
 * Ключи не записываются в таблицу.
 */

var RNP = {
  SHEET: 'РНП',
  DRR_SHEET: 'API Общий ДРР',
  SKU_SHEET: 'API Заказы и показы',
  SNAP_SHEET: 'API Остатки кластеры',
  DASH_SHEET: 'РНП ДАШБОРД',
  OZON: 'https://api-seller.ozon.ru',
  // имена свойств, под которыми ключи могли сохранить раньше
  ID_KEYS: ['OZON_CLIENT_ID', 'CLIENT_ID', 'ozonClientId', 'OZON_CLIENTID', 'Client-Id', 'clientId'],
  API_KEYS: ['OZON_API_KEY', 'API_KEY', 'ozonApiKey', 'OZON_APIKEY', 'Api-Key', 'apiKey']
};

/* ===================== МЕНЮ ===================== */

/**
 * Добавьте ДВЕ строки в своё меню в onOpen (см. инструкцию):
 *   .addItem('📈 График товара', 'rnpOpenChart')
 *   .addItem('📦 Остатки', 'rnpOpenStocks')
 * Если своего onOpen нет — эта функция сама создаёт отдельное меню.
 */
function rnpAddMenu() {
  SpreadsheetApp.getUi()
    .createMenu('📈 Графики')
    .addItem('📈 График товара', 'rnpOpenChart')
    .addItem('📦 Остатки', 'rnpOpenStocks')
    .addSeparator()
    .addItem('Ключи Ozon для остатков', 'rnpSetKeys')
    .addItem('Включить ежедневный снимок остатков', 'rnpInstallTrigger')
    .addToUi();
}

function rnpOpenChart() {
  var html = HtmlService.createHtmlOutput(rnpHtml_('chart', rnpDefaultArticle_()))
    .setWidth(1220).setHeight(900);
  SpreadsheetApp.getUi().showModelessDialog(html, 'График товара');
}

function rnpOpenStocks() {
  var html = HtmlService.createHtmlOutput(rnpHtml_('stocks', rnpDefaultArticle_()))
    .setWidth(1220).setHeight(820);
  SpreadsheetApp.getUi().showModelessDialog(html, 'Остатки');
}

/* ===================== КЛЮЧИ OZON ===================== */

function rnpKeys_() {
  var p = PropertiesService.getScriptProperties().getProperties();
  var d = PropertiesService.getDocumentProperties().getProperties();
  function pick(names) {
    for (var i = 0; i < names.length; i++) {
      if (p[names[i]]) return String(p[names[i]]).trim();
      if (d[names[i]]) return String(d[names[i]]).trim();
    }
    return '';
  }
  var id = pick(RNP.ID_KEYS), key = pick(RNP.API_KEYS);
  return id && key ? {id: id, key: key} : null;
}

function rnpSetKeys() {
  var ui = SpreadsheetApp.getUi();
  if (rnpKeys_()) {
    var r = ui.alert('Ключи Ozon уже найдены в свойствах скрипта. Заменить?', ui.ButtonSet.YES_NO);
    if (r !== ui.Button.YES) return;
  }
  var a = ui.prompt('Ozon Seller API', 'Client-Id:', ui.ButtonSet.OK_CANCEL);
  if (a.getSelectedButton() !== ui.Button.OK) return;
  var b = ui.prompt('Ozon Seller API', 'Api-Key (только чтение достаточно):', ui.ButtonSet.OK_CANCEL);
  if (b.getSelectedButton() !== ui.Button.OK) return;
  PropertiesService.getScriptProperties().setProperties({
    OZON_CLIENT_ID: a.getResponseText().trim(),
    OZON_API_KEY: b.getResponseText().trim()
  });
  ui.alert('Сохранено в свойствах скрипта. В таблицу ключи не записаны.');
}

function rnpOzon_(path, body) {
  var k = rnpKeys_();
  if (!k) throw new Error('Нет ключей Ozon. Меню «📈 Графики» → «Ключи Ozon для остатков».');
  var opt = {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: {'Client-Id': k.id, 'Api-Key': k.key},
    payload: JSON.stringify(body || {})
  };
  for (var attempt = 0; attempt < 4; attempt++) {
    var r = UrlFetchApp.fetch(RNP.OZON + path, opt);
    var c = r.getResponseCode();
    if (c === 200) return JSON.parse(r.getContentText());
    if (c === 429 || c >= 500) { Utilities.sleep(4000 * (attempt + 1)); continue; }
    throw new Error('Ozon ' + path + ': ' + c + ' ' + r.getContentText().slice(0, 200));
  }
  throw new Error('Ozon ' + path + ': лимит запросов, попробуйте через минуту');
}

/* ===================== ОБЩЕЕ ===================== */

function rnpNum_(v) {
  if (v === '' || v === null || v === undefined) return null;
  if (typeof v === 'number') return v;
  var s = String(v).replace(/\s/g, '').replace('₽', '');
  var pct = s.indexOf('%') >= 0;
  s = s.replace('%', '').replace(',', '.');
  if (s === '' || s === '-') return null;
  var n = Number(s);
  if (isNaN(n)) return null;
  return pct ? n / 100 : n;
}

function rnpIso_(d) {
  return Utilities.formatDate(d, 'Europe/Moscow', 'yyyy-MM-dd');
}

/** Даты из шапки листа РНП: «1.10», «31.10», «1.11.2026» → yyyy-mm-dd */
function rnpHeaderDates_(disp) {
  var out = [], yearHint = null, monthHint = null;
  for (var i = 0; i < disp.length; i++) {
    var m = String(disp[i]).match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/);
    if (m) { yearHint = +m[3]; monthHint = +m[2]; break; }
  }
  if (!yearHint) { yearHint = new Date().getFullYear(); monthHint = 12; }
  for (var j = 0; j < disp.length; j++) {
    var s = String(disp[j]).trim(), d = null;
    var a = s.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/), b = s.match(/^(\d{1,2})\.(\d{1,2})$/);
    if (a) d = [+a[3], +a[2], +a[1]];
    else if (b) d = [(+b[2] <= monthHint ? yearHint : yearHint - 1), +b[2], +b[1]];
    out.push(d ? d[0] + '-' + ('0' + d[1]).slice(-2) + '-' + ('0' + d[2]).slice(-2) : null);
  }
  return out;
}

/** Артикул по умолчанию: строка активной ячейки на листе РНП, иначе дашборд B4. */
function rnpDefaultArticle_() {
  try {
    var ss = SpreadsheetApp.getActive(), sh = ss.getActiveSheet();
    if (sh.getName() === RNP.SHEET) {
      var row = sh.getActiveCell().getRow();
      if (row >= 2) {
        var col = sh.getRange(2, 1, row - 1, 1).getValues();
        for (var i = col.length - 1; i >= 0; i--) if (String(col[i][0]).trim()) return String(col[i][0]).trim();
      }
    }
    var dash = ss.getSheetByName(RNP.DASH_SHEET);
    if (dash) return String(dash.getRange('B4').getValue() || '').trim();
  } catch (e) {}
  return '';
}

/** Список артикулов листа РНП (для выпадающего списка). */
function rnpArticles() {
  var sh = SpreadsheetApp.getActive().getSheetByName(RNP.SHEET);
  var last = sh.getLastRow();
  var col = sh.getRange(2, 1, last - 1, 3).getValues();
  var out = [];
  col.forEach(function (r) { if (String(r[0]).trim()) out.push({a: String(r[0]).trim(), c: String(r[2] || '')}); });
  return out;
}

/** Ozon SKU по артикулу (из листа «API Заказы и показы», последние строки). */
function rnpSkuMap_() {
  var cache = CacheService.getDocumentCache();
  var hit = cache.get('rnp_sku_map');
  if (hit) return JSON.parse(hit);
  var sh = SpreadsheetApp.getActive().getSheetByName(RNP.SKU_SHEET);
  var map = {};
  if (sh && sh.getLastRow() > 1) {
    var n = Math.min(sh.getLastRow() - 1, 6000);
    sh.getRange(sh.getLastRow() - n + 1, 2, n, 2).getValues().forEach(function (r) {
      if (r[0] && r[1]) map[String(r[1]).trim()] = String(r[0]).trim();
    });
  }
  try { cache.put('rnp_sku_map', JSON.stringify(map), 3600); } catch (e) {}
  return map;
}

/* ===================== ДАННЫЕ ДЛЯ ГРАФИКА ===================== */

var RNP_METRICS = {
  'Заказано товаров, шт.': 'orders',
  'Возвраты (на дату возврата), шт.': 'returns',
  'Моя цена продажи, ₽': 'price',
  'Место в выдаче': 'pos',
  'ДРР (аналитика продаж)': 'drr',
  'Клики всего': 'clicks',
  'Показы всего': 'views',
  'Чистая прибыль на 1 шт., ₽': 'profit',
  'Остаток ФБО, шт.': 'fbo'
};

/**
 * Данные по артикулу за период.
 * @param {string} art артикул (как в колонке A листа РНП)
 * @param {string} from yyyy-mm-dd
 * @param {string} to yyyy-mm-dd
 */
function rnpChartData(art, from, to) {
  art = String(art || '').trim();
  if (!art) return {err: 'Выберите артикул'};
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(RNP.SHEET);
  var last = sh.getLastRow(), lastCol = sh.getLastColumn();
  var colA = sh.getRange(1, 1, last, 1).getValues();
  var start = -1, end = last;
  for (var i = 1; i < colA.length; i++) {
    var v = String(colA[i][0]).trim();
    if (start < 0 && v === art) { start = i + 1; continue; }
    if (start > 0 && v) { end = i; break; }
  }
  if (start < 0) return {err: 'Артикул ' + art + ' не найден на листе РНП'};

  var dates = rnpHeaderDates_(sh.getRange(1, 5, 1, lastCol - 4).getDisplayValues()[0]);
  var block = sh.getRange(start, 1, end - start + 1, lastCol).getValues();
  var category = String(block[0][2] || '');

  var idx = [];
  dates.forEach(function (d, k) { if (d && d >= from && d <= to) idx.push(k); });
  var outDates = idx.map(function (k) { return dates[k]; });

  var series = {};
  block.forEach(function (r) {
    var key = RNP_METRICS[String(r[3]).trim()];
    if (!key) return;
    series[key] = idx.map(function (k) { return rnpNum_(r[4 + k]); });
  });

  // Реклама ₽ и сумма заказов ₽ по дням — лист «API Общий ДРР»
  var spend = outDates.map(function () { return null; });
  var ordSum = outDates.map(function () { return null; });
  var pos = {};
  outDates.forEach(function (d, k) { pos[d] = k; });
  var dr = ss.getSheetByName(RNP.DRR_SHEET);
  if (dr && dr.getLastRow() > 1) {
    var rows = dr.getRange(2, 1, dr.getLastRow() - 1, 4).getValues();
    rows.forEach(function (r) {
      if (String(r[1]).trim() !== art) return;
      var d = r[0] instanceof Date ? rnpIso_(r[0]) : String(r[0]).slice(0, 10);
      if (!(d in pos)) return;
      spend[pos[d]] = rnpNum_(r[2]);
      ordSum[pos[d]] = rnpNum_(r[3]);
    });
  }

  // Акции и остатки по кластерам — из ежедневных снимков (если включены)
  var snap = rnpSnapHistory_(art, from, to);

  // Текущие акции из API — показываем, если ключи есть
  var actionsNow = [];
  try { actionsNow = rnpActionsForArticle_(art); } catch (e) { actionsNow = []; }

  return {
    art: art, category: category, dates: outDates, s: series,
    spend: spend, ordSum: ordSum,
    actionsHist: snap.actions, actionsNow: actionsNow,
    stock: snap.stock, sku: rnpSkuMap_()[art] || ''
  };
}

/* ===================== АКЦИИ ===================== */

/** Акции, в которых артикул участвует сейчас: [{title, start, end}] */
function rnpActionsForArticle_(art) {
  var cache = CacheService.getDocumentCache();
  var hit = cache.get('rnp_actions');
  var byOffer;
  if (hit) byOffer = JSON.parse(hit);
  else {
    byOffer = rnpActionsByOffer_();
    try { cache.put('rnp_actions', JSON.stringify(byOffer), 1800); } catch (e) {}
  }
  return byOffer[art] || [];
}

/** Все текущие акции → {offer_id: [{title,start,end}]} */
function rnpActionsByOffer_() {
  var k = rnpKeys_();
  if (!k) return {};
  var r = UrlFetchApp.fetch(RNP.OZON + '/v1/actions', {
    method: 'get', muteHttpExceptions: true, headers: {'Client-Id': k.id, 'Api-Key': k.key}
  });
  if (r.getResponseCode() !== 200) return {};
  var actions = (JSON.parse(r.getContentText()).result || []).filter(function (a) { return a.is_participating; });
  var pidToOffer = rnpProductIdMap_();
  var out = {};
  actions.forEach(function (a) {
    var lastId = '', guard = 0;
    do {
      var j = rnpOzon_('/v1/actions/products', {action_id: a.id, limit: 100, last_id: lastId});
      var res = j.result || {};
      (res.products || []).forEach(function (p) {
        var off = pidToOffer[String(p.id)];
        if (!off) return;
        (out[off] = out[off] || []).push({
          title: a.title, start: String(a.date_start).slice(0, 10), end: String(a.date_end).slice(0, 10)
        });
      });
      lastId = res.last_id || '';
      guard++;
    } while (lastId && (res.products || []).length === 100 && guard < 30);
  });
  return out;
}

/** product_id → offer_id по всем товарам кабинета */
function rnpProductIdMap_() {
  var map = {}, cursor = '', guard = 0;
  do {
    var j = rnpOzon_('/v4/product/info/stocks', {filter: {visibility: 'ALL'}, limit: 1000, cursor: cursor});
    (j.items || []).forEach(function (it) { map[String(it.product_id)] = it.offer_id; });
    cursor = j.cursor || '';
    guard++;
  } while (cursor && (j.items || []).length === 1000 && guard < 20);
  return map;
}

/* ===================== ОСТАТКИ ===================== */

/**
 * Снимок остатков на сегодня: по каждому SKU — кластеры FBO (доступно к продаже), FBS, акции.
 * Пишет одну строку на SKU в лист «API Остатки кластеры». Повторный запуск в тот же день перезаписывает день.
 */
function rnpSnapshot() {
  var ss = SpreadsheetApp.getActive();
  var today = rnpIso_(new Date());
  var stocks = rnpStocksAll_();          // offer_id → {sku, fbo, fbs, product_id}
  var offers = Object.keys(stocks);
  var skus = offers.map(function (o) { return stocks[o].sku; }).filter(String);
  var clusters = rnpClusterStocks_(skus); // sku → {cluster: {q, ads, idc}}
  var actions = {};
  try { actions = rnpActionsByOffer_(); } catch (e) {}

  var sh = ss.getSheetByName(RNP.SNAP_SHEET);
  if (!sh) {
    sh = ss.insertSheet(RNP.SNAP_SHEET);
    sh.appendRow(['Дата', 'Ozon SKU', 'Артикул', 'FBO всего', 'FBS', 'Кластеры (JSON)', 'Акции (JSON)', 'Проверено']);
    sh.setFrozenRows(1);
  }
  // удалить строки этого дня, если уже были
  if (sh.getLastRow() > 1) {
    var col = sh.getRange(2, 1, sh.getLastRow() - 1, 1).getDisplayValues();
    var firstToday = -1;
    for (var i = 0; i < col.length; i++) if (col[i][0] === today) { firstToday = i + 2; break; }
    if (firstToday > 0) sh.deleteRows(firstToday, sh.getLastRow() - firstToday + 1);
  }
  var now = Utilities.formatDate(new Date(), 'Asia/Yekaterinburg', 'yyyy-MM-dd HH:mm:ss');
  var rows = offers.map(function (o) {
    var s = stocks[o], c = clusters[s.sku] || {};
    return [today, String(s.sku), o, s.fbo, s.fbs, JSON.stringify(c), JSON.stringify((actions[o] || []).map(function (a) { return a.title; })), now];
  });
  if (rows.length) {
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, 8).setNumberFormat('@').setValues(rows);
  }
  CacheService.getDocumentCache().remove('rnp_actions');
}

/** v4/product/info/stocks → offer_id: {sku, fbo, fbs} */
function rnpStocksAll_() {
  var out = {}, cursor = '', guard = 0;
  do {
    var j = rnpOzon_('/v4/product/info/stocks', {filter: {visibility: 'ALL'}, limit: 1000, cursor: cursor});
    (j.items || []).forEach(function (it) {
      var o = {sku: '', fbo: 0, fbs: 0, product_id: it.product_id};
      (it.stocks || []).forEach(function (s) {
        if (s.sku && !o.sku) o.sku = s.sku;
        if (s.type === 'fbo') o.fbo += s.present || 0;
        if (s.type === 'fbs' || s.type === 'rfbs') o.fbs += s.present || 0;
      });
      out[it.offer_id] = o;
    });
    cursor = j.cursor || '';
    guard++;
  } while (cursor && (j.items || []).length === 1000 && guard < 20);
  return out;
}

/** v1/analytics/stocks (по 100 SKU) → sku: {кластер: {q: доступно, t: в пути, ads: продаж/день, idc: дней запаса}} */
function rnpClusterStocks_(skus) {
  var out = {};
  for (var i = 0; i < skus.length; i += 100) {
    var part = skus.slice(i, i + 100).map(String);
    var j = rnpOzon_('/v1/analytics/stocks', {skus: part});
    (j.items || []).forEach(function (it) {
      var m = out[it.sku] = out[it.sku] || {};
      var c = m[it.cluster_name] = m[it.cluster_name] || {q: 0, t: 0, ads: 0, idc: 0};
      c.q += it.available_stock_count || 0;
      c.t += (it.transit_stock_count || 0) + (it.requested_stock_count || 0);
      c.ads = Math.round((it.ads_cluster || 0) * 100) / 100;
      c.idc = it.idc_cluster || 0;
    });
    if (i + 100 < skus.length) Utilities.sleep(2500);
  }
  return out;
}

/** История снимков по артикулу: {stock:{dates, clusters:{name:[q]}, fbs:[]}, actions:{date:[titles]}} */
function rnpSnapHistory_(art, from, to) {
  var sh = SpreadsheetApp.getActive().getSheetByName(RNP.SNAP_SHEET);
  var res = {stock: {dates: [], clusters: {}, fbs: [], fbo: []}, actions: {}};
  if (!sh || sh.getLastRow() < 2) return res;
  var rows = sh.getRange(2, 1, sh.getLastRow() - 1, 7).getDisplayValues();
  var picked = rows.filter(function (r) { return r[2] === art && r[0] >= from && r[0] <= to; });
  picked.sort(function (a, b) { return a[0] < b[0] ? -1 : 1; });
  picked.forEach(function (r, k) {
    res.stock.dates.push(r[0]);
    res.stock.fbo.push(rnpNum_(r[3]));
    res.stock.fbs.push(rnpNum_(r[4]));
    var c = {};
    try { c = JSON.parse(r[5] || '{}'); } catch (e) {}
    Object.keys(c).forEach(function (name) {
      var arr = res.stock.clusters[name] = res.stock.clusters[name] || picked.map(function () { return null; });
      arr[k] = c[name].q;
    });
    try { res.actions[r[0]] = JSON.parse(r[6] || '[]'); } catch (e) {}
  });
  return res;
}

/** Данные окна «Остатки»: живой срез по кластерам + история из снимков. */
function rnpStocksData(art, from, to) {
  art = String(art || '').trim();
  if (!art) return {err: 'Выберите артикул'};
  var sku = rnpSkuMap_()[art];
  var live = null, liveErr = '';
  try {
    if (!sku) {
      var j0 = rnpOzon_('/v4/product/info/stocks', {filter: {offer_id: [art], visibility: 'ALL'}, limit: 10});
      ((j0.items || [])[0] && (j0.items[0].stocks || [])).forEach(function (s) { if (s.sku && !sku) sku = String(s.sku); });
    }
    if (sku) {
      var j = rnpOzon_('/v4/product/info/stocks', {filter: {offer_id: [art], visibility: 'ALL'}, limit: 10});
      var fbo = 0, fbs = 0;
      ((j.items || [])[0] ? j.items[0].stocks : []).forEach(function (s) {
        if (s.type === 'fbo') fbo += s.present || 0;
        if (s.type === 'fbs' || s.type === 'rfbs') fbs += s.present || 0;
      });
      live = {date: rnpIso_(new Date()), fbo: fbo, fbs: fbs, clusters: rnpClusterStocks_([sku])[sku] || {}};
    }
  } catch (e) { liveErr = String(e.message || e); }

  var hist = rnpSnapHistory_(art, from, to);
  // FBO всего по дням есть и на листе РНП — подставим для дней без снимков
  var chart = rnpChartData(art, from, to);
  return {
    art: art, sku: sku || '', category: chart.category || '',
    live: live, liveErr: liveErr,
    hist: hist.stock,
    rnpDates: chart.dates || [], rnpFbo: (chart.s && chart.s.fbo) || [], rnpOrders: (chart.s && chart.s.orders) || []
  };
}

/** Поставить ежедневный снимок остатков (06:00) — один раз. */
function rnpInstallTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'rnpSnapshot') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('rnpSnapshot').timeBased().everyDays(1).atHour(6).inTimezone('Europe/Moscow').create();
  var ui = SpreadsheetApp.getUi();
  try {
    rnpSnapshot();
    ui.alert('Готово. Снимок остатков сделан сейчас и дальше будет делаться каждый день в 6:00 МСК (лист «API Остатки кластеры»).');
  } catch (e) {
    ui.alert('Расписание поставлено, но первый снимок не получился: ' + e.message);
  }
}

/* ===================== HTML ===================== */

function rnpHtml_(mode, art) {
  var today = new Date();
  var to = rnpIso_(today);
  var fromD = new Date(today.getFullYear(), today.getMonth(), 1);
  if (today.getDate() <= 3) fromD = new Date(today.getFullYear(), today.getMonth() - 1, 1);
  var boot = {mode: mode, art: art || '', from: rnpIso_(fromD), to: to};
  return RNP_HTML.replace('/*BOOT*/null', JSON.stringify(boot).replace(/</g, '\\u003c'));
}

var RNP_HTML = String.raw`<!doctype html><html><head><meta charset="utf-8">
<style>
:root{--bg:#0b1d45;--card:#10275a;--line:#24407e;--text:#e8eefc;--muted:#9fb2db;--orders:#4ea1ff;--price:#e0703a;--profit:#f2f2f2;--drr:#c9b8ff;--pos:#6fe0b8;--fbo:#ffd166;--bar:#c8d3ee;--red:#e5484d}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 Arial,Helvetica,sans-serif}
.wrap{padding:14px 18px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
h1{font-size:19px;margin:0}.sub{color:var(--muted);font-size:12px}.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.ctl{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 8px}
input,select,button{background:#0c2150;color:var(--text);border:1px solid #2f4f93;border-radius:7px;padding:6px 9px;font:13px Arial}
input[list]{width:250px}button{cursor:pointer;background:#2b5fd9;border-color:#2b5fd9}button:hover{filter:brightness(1.1)}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:6px 0}
.chip{display:flex;align-items:center;gap:6px;border:1px solid #3a5aa0;border-radius:16px;padding:4px 10px;cursor:pointer;user-select:none}
.chip input{margin:0}.sw{width:16px;height:3px;border-radius:2px;display:inline-block}
.note{color:var(--muted);font-size:11.5px;margin:4px 0}
svg text{fill:var(--muted);font:11px Arial}
#tip{position:fixed;pointer-events:none;background:#061331;border:1px solid #3a5aa0;border-radius:8px;padding:8px 10px;font-size:12px;display:none;max-width:330px;z-index:9;box-shadow:0 6px 18px rgba(0,0,0,.4)}
#tip b{color:#fff}#tip .r{display:flex;justify-content:space-between;gap:14px}
.ev{display:grid;grid-template-columns:26px 74px 1fr;gap:6px;padding:4px 0;border-bottom:1px solid #1d3570;align-items:center}
.num{width:20px;height:20px;border-radius:50%;background:#fff;color:#0b1d45;font-weight:bold;font-size:11px;display:flex;align-items:center;justify-content:center}
.num.bad{background:var(--red);color:#fff}
h3{font-size:14px;margin:16px 0 6px}
table.hm{border-collapse:separate;border-spacing:2px}table.hm td,table.hm th{font-size:11px;text-align:center;min-width:30px;padding:3px 2px;border-radius:4px}
table.hm th{color:var(--muted);font-weight:normal}table.hm td.lbl{text-align:right;color:var(--text);min-width:130px;padding-right:6px;white-space:nowrap;background:none}
table.t{border-collapse:collapse;width:100%}table.t th,table.t td{padding:5px 8px;border-bottom:1px solid #1d3570;text-align:right}table.t th:first-child,table.t td:first-child{text-align:left}
table.t th{color:var(--muted);font-weight:normal}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}.kpi{background:#0c2150;border:1px solid #24407e;border-radius:10px;padding:8px 12px;min-width:140px}.kpi b{font-size:18px;display:block}
.err{color:#ffb4b4;margin:10px 0}.load{color:var(--muted);margin:20px 0}
</style></head><body><div class="wrap"><div class="card">
<div class="top"><h1 id="ttl">…</h1><div class="sub" id="subt"></div></div>
<div class="ctl">Артикул <input id="art" list="arts" placeholder="OZ066"><datalist id="arts"></datalist>
Месяц <select id="mon"></select> с <input type="date" id="from"> по <input type="date" id="to"> <button id="go">Показать</button></div>
<div id="chips" class="chips"></div><div id="legend" class="note"></div>
<div id="main"><div class="load">Загружаю…</div></div>
</div></div><div id="tip"></div>
<script>
var BOOT=/*BOOT*/null;
var MON=['январь','февраль','март','апрель','май','июнь','июль','август','сентябрь','октябрь','ноябрь','декабрь'];
var $=function(id){return document.getElementById(id)};
var D=null, SHOW={orders:1,price:1,profit:1,drr:1,spend:1,pos:0,fbo:1,actions:1,events:1};
var CH=[['orders','Заказы, шт','var(--orders)'],['price','Моя цена, ₽','var(--price)'],['profit','Прибыль на 1 шт, ₽','var(--profit)'],['drr','ДРР, %','var(--drr)'],['spend','Реклама, ₽','var(--bar)'],['pos','Место в выдаче','var(--pos)'],['fbo','Остаток FBO','var(--fbo)'],['actions','Акции','#1f9d6b'],['events','События','#fff']];
function fmt(n,d){if(n===null||n===undefined||isNaN(n))return '—';return Number(n).toLocaleString('ru-RU',{maximumFractionDigits:d||0})}
function dd(s){var p=s.split('-');return +p[2]+'.'+p[1]}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function api(fn,args,ok){if(window.google&&google.script){google.script.run.withSuccessHandler(ok).withFailureHandler(function(e){$('main').innerHTML='<div class="err">'+esc(e.message||e)+'</div>'})[fn].apply(null,args)}else{window.MOCK[fn].apply(null,args.concat([ok]))}}
function init(){
 $('ttl').textContent=BOOT.mode==='chart'?'Заказы в день и что их двигало':'Остатки: FBO по кластерам и FBS';
 $('art').value=BOOT.art;$('from').value=BOOT.from;$('to').value=BOOT.to;
 var t=new Date(BOOT.to),o='';for(var i=0;i<12;i++){var d=new Date(t.getFullYear(),t.getMonth()-i,1);o+='<option value="'+d.getFullYear()+'-'+(d.getMonth()+1)+'">'+MON[d.getMonth()]+' '+d.getFullYear()+'</option>'}
 $('mon').innerHTML='<option value="">— период вручную —</option>'+o;
 $('mon').onchange=function(){if(!this.value)return;var p=this.value.split('-'),y=+p[0],m=+p[1];var a=new Date(Date.UTC(y,m-1,1)),b=new Date(Date.UTC(y,m,0));var td=new Date(BOOT.to);
  $('from').value=a.toISOString().slice(0,10);$('to').value=(b>td?td:b).toISOString().slice(0,10);load()};
 $('go').onclick=load;$('art').onchange=load;
 if(BOOT.mode==='chart'){$('chips').innerHTML=CH.map(function(c){return '<label class="chip"><input type="checkbox" data-k="'+c[0]+'"'+(SHOW[c[0]]?' checked':'')+'><span class="sw" style="background:'+c[2]+'"></span>'+c[1]+'</label>'}).join('');
  [].forEach.call(document.querySelectorAll('.chip input'),function(x){x.onchange=function(){SHOW[this.dataset.k]=this.checked?1:0;if(D)draw()}})}
 api('rnpArticles',[],function(list){$('arts').innerHTML=list.map(function(x){return '<option value="'+esc(x.a)+'">'+esc(x.c)+'</option>'}).join('')});
 load();
}
function load(){var a=$('art').value.trim();if(!a){$('main').innerHTML='<div class="err">Выберите артикул</div>';return}
 $('main').innerHTML='<div class="load">Загружаю '+esc(a)+'…</div>';
 api(BOOT.mode==='chart'?'rnpChartData':'rnpStocksData',[a,$('from').value,$('to').value],function(d){if(d.err){$('main').innerHTML='<div class="err">'+esc(d.err)+'</div>';return}D=d;BOOT.mode==='chart'?draw():drawStocks()})}

/* ---------- события ---------- */
function avg(a,i,j){var s=0,n=0;for(var k=Math.max(0,i);k<=Math.min(a.length-1,j);k++){if(a[k]!==null&&a[k]!==undefined){s+=a[k];n++}}return n?s/n:null}
function events(){var s=D.s,n=D.dates.length,o=s.orders||[],out=[];
 function add(k,t){var e=out.filter(function(x){return x.k===k})[0];if(!e){e={k:k,t:[]};out.push(e)}e.t.push(t)}
 for(var k=1;k<n;k++){
  var p0=(s.price||[])[k-1],p1=(s.price||[])[k];if(p0&&p1&&Math.abs(p1-p0)/p0>=0.03)add(k,'Цена '+fmt(p0)+' → '+fmt(p1)+' ₽ ('+(p1>p0?'+':'')+fmt((p1-p0)/p0*100)+'%)');
  var a0=D.spend[k-1],a1=D.spend[k];
  if(a0>0&&!(a1>0)&&a1!==null)add(k,'Реклама остановлена (вчера '+fmt(a0)+' ₽)');
  else if(!(a0>0)&&a1>0&&a0!==null)add(k,'Реклама включена: '+fmt(a1)+' ₽');
  else if(a0>300&&a1!==null){var ch=(a1-a0)/a0;if(ch>=0.6||ch<=-0.45)add(k,'Расход рекламы '+fmt(a0)+' → '+fmt(a1)+' ₽')}
  var f0=(s.fbo||[])[k-1],f1=(s.fbo||[])[k],sp=avg(o,k-7,k-1);
  if(f0>0&&f1!==null&&f1<=0)add(k,'FBO закончился');
  else if(sp&&f0!==null&&f1!==null&&f0>=sp*7&&f1<sp*7&&f1>0)add(k,'Остаток FBO меньше чем на 7 дней ('+fmt(f1)+' шт)');
  else if(f0!==null&&f1!==null&&f1-f0>=Math.max(30,f0*0.25))add(k,'Поставка на FBO: +'+fmt(f1-f0)+' шт');
  var q0=(s.pos||[])[k-1],q1=(s.pos||[])[k];if(q0&&q1&&Math.abs(q1-q0)>=5&&Math.abs(q1-q0)/q0>=0.4)add(k,'Место в выдаче '+fmt(q0)+' → '+fmt(q1));
 }
 acts().forEach(function(a){if(a.i0>0)add(a.i0,'Старт акции: '+a.t);if(a.i1<n-1&&a.endsIn)add(a.i1+1,'Конец акции: '+a.t)});
 out.sort(function(a,b){return a.k-b.k});
 out.forEach(function(e,i){e.n=i+1;var before=avg(o,e.k-3,e.k-1),after=avg(o,e.k,e.k+2);e.bad=before!==null&&after!==null&&before>=3&&after<before*0.8;e.delta=before&&after!==null?(after-before)/before:null});
 return out}
function acts(){var n=D.dates.length,res=[],seen={};if(!n)return res;var f=D.dates[0],l=D.dates[n-1];
 (D.actionsNow||[]).forEach(function(a){if(a.end<f||a.start>l)return;var key=a.title;if(seen[key])return;seen[key]=1;
  var i0=0,i1=n-1;D.dates.forEach(function(d,i){if(d<a.start)i0=i+1;if(d<=a.end)i1=i});res.push({t:a.title,i0:Math.min(i0,n-1),i1:i1,endsIn:a.end<=l,src:'сейчас'})});
 var h=D.actionsHist||{},titles={};Object.keys(h).forEach(function(d){(h[d]||[]).forEach(function(t){(titles[t]=titles[t]||[]).push(d)})});
 Object.keys(titles).forEach(function(t){var idx=titles[t].map(function(d){return D.dates.indexOf(d)}).filter(function(i){return i>=0}).sort(function(a,b){return a-b});
  var st=null,pr=null;idx.concat([1e9]).forEach(function(i){if(st===null){st=i;pr=i;return}if(i===pr+1){pr=i;return}
   if(!res.some(function(r){return r.t===t&&r.i0<=st&&r.i1>=pr}))res.push({t:t,i0:st,i1:pr,endsIn:pr<n-1,src:'снимок'});st=i;pr=i})});
 return res}

/* ---------- график ---------- */
function draw(){var n=D.dates.length;
 $('subt').textContent=D.art+(D.category?' · '+D.category:'')+' · '+(n?dd(D.dates[0])+' – '+dd(D.dates[n-1]):'нет дат');
 if(!n){$('main').innerHTML='<div class="err">За выбранный период на листе РНП нет данных</div>';return}
 var W=1150,H=380,L=44,R=56,T=26,B=28,pw=W-L-R,ph=H-T-B,step=pw/Math.max(1,n-1),bw=Math.max(4,Math.min(26,step*0.62));
 var X=function(i){return L+(n===1?pw/2:i*step)};
 var s=D.s,o=s.orders||[];
 function mx(a){var m=0;(a||[]).forEach(function(v){if(v!==null&&v>m)m=v});return m}
 function mn(a){var m=null;(a||[]).forEach(function(v){if(v!==null&&v>0&&(m===null||v<m))m=v});return m}
 var oMax=nice(mx(o)*1.15||10),pMax=mx(s.price),pMin=mn(s.price)||0;var pLo=pMin*0.9,pHi=pMax*1.05||1;
 function Yo(v){return T+ph-v/oMax*ph}function Yp(v){return T+ph-(v-pLo)/(pHi-pLo||1)*ph*0.9-ph*0.05}
 function Ys(a,lo,hi){return function(v){return T+ph-(v-lo)/((hi-lo)||1)*ph*0.85-ph*0.05}}
 var g='';
 for(var k=0;k<=4;k++){var y=T+ph*k/4;g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y+'" y2="'+y+'" stroke="#1e386f"/><text x="'+(L-6)+'" y="'+(y+4)+'" text-anchor="end">'+fmt(oMax*(1-k/4))+'</text>';
  if(SHOW.price&&pMax)g+='<text x="'+(W-R+6)+'" y="'+(y+4)+'">'+fmt(pHi-(pHi-pLo)*k/4)+'</text>'}
 var every=Math.ceil(n/16);D.dates.forEach(function(d,i){if(i%every===0||i===n-1)g+='<text x="'+X(i)+'" y="'+(H-8)+'" text-anchor="middle">'+dd(d)+'</text>'});
 // реклама — столбики
 if(SHOW.spend){var sm=mx(D.spend)||1;D.spend.forEach(function(v,i){if(!(v>0))return;var h=v/sm*ph*0.45;g+='<rect x="'+(X(i)-bw/2)+'" y="'+(T+ph-h)+'" width="'+bw+'" height="'+h+'" rx="2" fill="var(--bar)" opacity=".55"/>'});
  var im=D.spend.indexOf(sm);if(im>=0)g+='<text x="'+X(im)+'" y="'+(T+ph-ph*0.45-4)+'" text-anchor="middle" style="fill:#c8d3ee">'+fmt(sm)+' ₽</text>'}
 function path(a,Y,dash){var d='',on=false;(a||[]).forEach(function(v,i){if(v===null||v===undefined){on=false;return}d+=(on?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1);on=true});return d}
 function line(a,Y,col,w,dash){var p=path(a,Y);return p?'<path d="'+p+'" fill="none" stroke="'+col+'" stroke-width="'+(w||2)+'"'+(dash?' stroke-dasharray="'+dash+'"':'')+' stroke-linejoin="round"/>':''}
 function peak(a,Y,col,lbl,f){var m=-1e18,im=-1;(a||[]).forEach(function(v,i){if(v!==null&&v>m){m=v;im=i}});if(im<0)return '';return '<text x="'+X(im)+'" y="'+(Y(m)-6)+'" text-anchor="middle" style="fill:'+col+'">'+lbl+' '+f(m)+'</text>'}
 if(SHOW.fbo&&s.fbo){var fM=mx(s.fbo);var Yf=Ys(null,0,fM*1.1);g+=line(s.fbo,Yf,'var(--fbo)',1.5,'2 3')+peak(s.fbo,Yf,'#ffd166','FBO',function(v){return fmt(v)+' шт'})}
 if(SHOW.pos&&s.pos){var qM=mx(s.pos);var Yq=function(v){return T+ph*0.05+(v/(qM*1.1||1))*ph*0.85};g+=line(s.pos,Yq,'var(--pos)',1.5,'6 3')}
 if(SHOW.drr&&s.drr){var dM=mx(s.drr);var Yd=Ys(null,0,dM*1.05);g+=line(s.drr,Yd,'var(--drr)',1.6,'2 2')+peak(s.drr,Yd,'#c9b8ff','ДРР',function(v){return fmt(v*100,1)+'%'})}
 if(SHOW.profit&&s.profit){var vals=(s.profit||[]).filter(function(v){return v!==null});if(vals.length){var lo=Math.min.apply(null,vals.concat([0])),hi=Math.max.apply(null,vals.concat([1]));var Yr=Ys(null,lo,hi);
   g+=line(s.profit,Yr,'var(--profit)',1.8,'7 4');if(lo<0)g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Yr(0)+'" y2="'+Yr(0)+'" stroke="#fff" stroke-opacity=".25" stroke-dasharray="3 3"/>'}}
 if(SHOW.price&&pMax){g+=line(s.price,Yp,'var(--price)',2.2);(s.price||[]).forEach(function(v,i){if(v)g+='<circle cx="'+X(i)+'" cy="'+Yp(v)+'" r="2.4" fill="var(--price)"/>'})}
 if(SHOW.orders){var pa=path(o,Yo);if(pa){g+='<path d="'+pa+'" fill="none" stroke="var(--orders)" stroke-width="7" stroke-opacity=".18"/>'+line(o,Yo,'var(--orders)',2.6)}}
 var EV=events();
 if(SHOW.events)EV.forEach(function(e){var v=o[e.k]||0;var y=Yo(v);g+='<line x1="'+X(e.k)+'" x2="'+X(e.k)+'" y1="'+T+'" y2="'+(T+ph)+'" stroke="#fff" stroke-opacity=".18" stroke-dasharray="2 3"/><circle cx="'+X(e.k)+'" cy="'+y+'" r="10" fill="'+(e.bad?'var(--red)':'#fff')+'" stroke="#0b1d45" stroke-width="2"/><text x="'+X(e.k)+'" y="'+(y+4)+'" text-anchor="middle" style="fill:'+(e.bad?'#fff':'#0b1d45')+';font-weight:bold">'+e.n+'</text>'});
 g+='<text x="'+L+'" y="14" style="fill:#4ea1ff;font-weight:bold">Заказы, шт</text>';if(SHOW.price&&pMax)g+='<text x="'+(W-R+50)+'" y="14" text-anchor="end" style="fill:#e0703a;font-weight:bold">Цена, ₽</text>';
 g+='<rect id="hov" x="0" y="'+T+'" width="'+Math.max(6,step)+'" height="'+ph+'" fill="#fff" opacity="0"/><rect x="'+L+'" y="'+T+'" width="'+pw+'" height="'+ph+'" fill="transparent" id="hit"/>';
 var svg='<svg id="sv" viewBox="0 0 '+W+' '+H+'" width="100%">'+g+'</svg>';
 // акции
 var A=SHOW.actions?acts():[],ah='';
 if(SHOW.actions){if(A.length){var AH=24*A.length+22;ah='<svg viewBox="0 0 '+W+' '+AH+'" width="100%"><text x="'+L+'" y="13" style="fill:#e8eefc;font-weight:bold">Акции</text>';
  A.forEach(function(a,j){var x0=X(a.i0)-step/2,x1=X(a.i1)+step/2;x0=Math.max(L-6,x0);x1=Math.min(W-R+6,x1);var y=20+j*24;
   var lbl=esc(a.t.length>60?a.t.slice(0,58)+'…':a.t),wide=(x1-x0)>a.t.length*6.2;
   ah+='<rect x="'+x0+'" y="'+y+'" width="'+Math.max(14,x1-x0)+'" height="19" rx="5" fill="#1f9d6b"/>'+(wide?'<text x="'+(x0+6)+'" y="'+(y+13)+'" style="fill:#fff">'+lbl+'</text>':(x0>W/2?'<text x="'+(x0-6)+'" y="'+(y+13)+'" text-anchor="end" style="fill:#e8eefc">'+lbl+'</text>':'<text x="'+(x1+6)+'" y="'+(y+13)+'" style="fill:#e8eefc">'+lbl+'</text>'))});ah+='</svg>'}
  else ah='<div class="note">Акции: данных нет. Текущие акции подтянутся из Ozon API, когда скрипт найдёт ключи; историю даст ежедневный снимок.</div>'}
 // список событий
 var el='';if(SHOW.events&&EV.length){el='<h3>События (автоматически: цена, реклама, остаток, место в выдаче, акции)</h3>'+EV.map(function(e){var dl=e.delta===null?'':' <span class="sub">заказы 3 дня до/после: '+(e.delta>0?'+':'')+fmt(e.delta*100)+'%</span>';
  return '<div class="ev"><div class="num'+(e.bad?' bad':'')+'">'+e.n+'</div><div>'+dd(D.dates[e.k])+'</div><div>'+e.t.map(esc).join('; ')+dl+'</div></div>'}).join('')}
 // остатки (снимки)
 var st=stockHeat(D.stock,null,null);
 $('legend').innerHTML='Столбики — расход рекламы по артикулу за день (лист «API Общий ДРР»). Остальные линии — каждая в своём масштабе, подпись у максимума. <span style="color:#e5484d">●</span> — событие, после которого заказы за 3 дня упали больше чем на 20%. Наведите на день — покажу цифры.';
 $('main').innerHTML=svg+ah+el+'<h3>Остатки: FBO по кластерам и FBS</h3>'+st;
 hover(X,step,n,EV);
}
function nice(v){if(v<=0)return 10;var p=Math.pow(10,Math.floor(Math.log10(v))),m=v/p;return (m<=1?1:m<=2?2:m<=2.5?2.5:m<=5?5:10)*p}
function hover(X,step,n,EV){var sv=$('sv'),tip=$('tip');
 sv.onmousemove=function(ev){var r=sv.getBoundingClientRect(),sc=1150/r.width,x=(ev.clientX-r.left)*sc;var i=Math.round((x-X(0))/(step||1));if(n===1)i=0;if(i<0||i>=n){tip.style.display='none';return}
  var h=$('hov');h.setAttribute('x',X(i)-Math.max(6,step)/2);h.setAttribute('opacity','.06');
  var s=D.s,rows=[['Заказы',fmt((s.orders||[])[i])+' шт'],['Сумма заказов',fmt(D.ordSum[i])+' ₽'],['Моя цена',fmt((s.price||[])[i])+' ₽'],['Прибыль на 1 шт',fmt((s.profit||[])[i])+' ₽'],['Реклама',fmt(D.spend[i])+' ₽'],['ДРР',(s.drr||[])[i]!==null&&(s.drr||[])[i]!==undefined?fmt(s.drr[i]*100,1)+'%':'—'],['Показы / клики',fmt((s.views||[])[i])+' / '+fmt((s.clicks||[])[i])],['Место в выдаче',fmt((s.pos||[])[i])],['Возвраты',fmt((s.returns||[])[i])],['Остаток FBO',fmt((s.fbo||[])[i])+' шт']];
  var e=EV.filter(function(x){return x.k===i})[0];
  tip.innerHTML='<b>'+dd(D.dates[i])+'</b>'+rows.map(function(r){return '<div class="r"><span>'+r[0]+'</span><span>'+r[1]+'</span></div>'}).join('')+(e?'<div style="margin-top:5px;color:'+(e.bad?'#ffb4b4':'#fff')+'">'+e.n+'. '+e.t.map(esc).join('; ')+'</div>':'');
  tip.style.display='block';var tx=ev.clientX+16,ty=ev.clientY+10;if(tx+340>innerWidth)tx=ev.clientX-350;if(ty+260>innerHeight)ty=innerHeight-265;tip.style.left=tx+'px';tip.style.top=ty+'px'};
 sv.onmouseleave=function(){tip.style.display='none';$('hov').setAttribute('opacity','0')}}

/* ---------- остатки ---------- */
function cellColor(v,max){if(v===null||v===undefined)return 'background:#0c2150;color:#4d6499';var t=max?Math.min(1,v/max):0;
 var l=88-t*45;return 'background:hsl(217,70%,'+l+'%);color:'+(l<60?'#fff':'#0b1d45')}
function stockHeat(h,live,extraNote){h=h||{dates:[],clusters:{},fbs:[]};var dates=h.dates.slice(),cl={};Object.keys(h.clusters||{}).forEach(function(k){cl[k]=h.clusters[k].slice()});var fbs=(h.fbs||[]).slice();
 if(live&&dates.indexOf(live.date)<0){dates.push(live.date);Object.keys(cl).forEach(function(k){cl[k].push(null)});Object.keys(live.clusters||{}).forEach(function(k){if(!cl[k])cl[k]=dates.map(function(){return null});cl[k][dates.length-1]=live.clusters[k].q});fbs.push(live.fbs)}
 if(!dates.length)return '<div class="note">История по кластерам появится после включения ежедневного снимка: меню «📈 Графики» → «Включить ежедневный снимок остатков». FBO всего по дням — на графике выше (линия «Остаток FBO»).</div>';
 var names=Object.keys(cl).filter(function(k){return cl[k].some(function(v){return v>0})});
 names.sort(function(a,b){return (cl[b][cl[b].length-1]||0)-(cl[a][cl[a].length-1]||0)});
 var max=0;names.forEach(function(k){cl[k].forEach(function(v){if(v>max)max=v})});
 var hide=names.length>12?names.slice(12):[];names=names.slice(0,12);
 var tb='<table class="hm"><tr><th></th>'+dates.map(function(d){return '<th>'+dd(d)+'</th>'}).join('')+'</tr><tr><td class="lbl" style="color:#9fb2db">кластеров с остатком</td>'+dates.map(function(d,i){var c=Object.keys(cl).filter(function(k){return cl[k][i]>0}).length;return '<th>'+c+'</th>'}).join('')+'</tr>';
 names.forEach(function(k){tb+='<tr><td class="lbl">'+esc(k)+'</td>'+cl[k].map(function(v){return '<td style="'+cellColor(v,max)+'">'+(v===null?'·':fmt(v))+'</td>'}).join('')+'</tr>'});
 var fm=0;fbs.forEach(function(v){if(v>fm)fm=v});
 tb+='<tr><td class="lbl">FBS (свой)</td>'+fbs.map(function(v){return '<td style="'+cellColor(v,fm)+'">'+(v===null?'·':fmt(v))+'</td>'}).join('')+'</tr></table>';
 if(hide.length)tb+='<div class="note">и ещё '+hide.length+' кластер(ов) с меньшим остатком: '+hide.map(esc).join(', ')+'</div>';
 return '<div style="overflow-x:auto">'+tb+'</div>'}
function drawStocks(){var d=D;$('subt').textContent=d.art+(d.category?' · '+d.category:'')+(d.sku?' · SKU '+d.sku:'');
 var lv=d.live,html='';
 if(lv){var tot=0,tr=0,ads=0;var rows=Object.keys(lv.clusters).map(function(k){var c=lv.clusters[k];tot+=c.q;tr+=c.t;ads+=c.ads;return [k,c]}).filter(function(r){return r[1].q>0||r[1].t>0||r[1].ads>0});
  rows.sort(function(a,b){return b[1].q-a[1].q});var days=ads?tot/ads:null;
  html+='<div class="kpis"><div class="kpi"><span class="sub">FBO доступно сейчас</span><b>'+fmt(tot)+' шт</b></div><div class="kpi"><span class="sub">FBS (свой склад)</span><b>'+fmt(lv.fbs)+' шт</b></div><div class="kpi"><span class="sub">В пути / заявлено</span><b>'+fmt(tr)+' шт</b></div><div class="kpi"><span class="sub">Продаж в день (Ozon, по кластерам)</span><b>'+fmt(ads,1)+'</b></div><div class="kpi"><span class="sub">Хватит FBO</span><b>'+(days===null?'—':fmt(days)+' дн')+'</b></div></div>';
  html+='<h3>По кластерам сейчас (Ozon API, '+dd(lv.date)+')</h3><table class="t"><tr><th>Кластер</th><th>Доступно, шт</th><th>В пути, шт</th><th>Продаж/день</th><th>Дней запаса</th><th></th></tr>'+rows.map(function(r){var c=r[1],dz=c.ads?c.q/c.ads:null;var flag=c.ads>0.3&&(dz===null||dz<14)?'<span style="color:#ffb4b4">мало</span>':(c.ads<0.05&&c.q>30?'<span style="color:#ffd166">лежит</span>':'');
   return '<tr><td>'+esc(r[0])+'</td><td>'+fmt(c.q)+'</td><td>'+fmt(c.t)+'</td><td>'+fmt(c.ads,2)+'</td><td>'+(dz===null?'—':fmt(dz))+'</td><td>'+flag+'</td></tr>'}).join('')+'</table><div class="note">«мало» — продаётся от 0,3 шт/день и запаса меньше 14 дней; «лежит» — больше 30 шт без продаж. Продажи в день — среднее Ozon по кластеру.</div>'}
 else html+='<div class="err">Живой срез из Ozon не получен'+(d.liveErr?': '+esc(d.liveErr):'')+'.</div>';
 // линия FBO всего по дням с листа РНП
 var f=d.rnpFbo||[],ds=d.rnpDates||[];if(ds.length){var W=1150,H=110,L=60,R=20,m=0;f.forEach(function(v){if(v>m)m=v});var st=(W-L-R)/Math.max(1,ds.length-1),p='',on=false;
  f.forEach(function(v,i){if(v===null){on=false;return}p+=(on?'L':'M')+(L+i*st).toFixed(1)+' '+(H-18-(v/(m||1))*(H-34)).toFixed(1);on=true});
  html+='<h3>FBO всего по дням (лист РНП)</h3><svg viewBox="0 0 '+W+' '+H+'" width="100%"><text x="4" y="20">'+fmt(m)+' шт</text><path d="'+p+'" fill="none" stroke="#ffd166" stroke-width="2"/>'+ds.map(function(x,i){return i%Math.ceil(ds.length/16)===0?'<text x="'+(L+i*st)+'" y="'+(H-3)+'" text-anchor="middle">'+dd(x)+'</text>':''}).join('')+'</svg>'}
 html+='<h3>Остатки по кластерам по дням</h3>'+stockHeat(d.hist,lv);
 $('legend').innerHTML='Источник: Ozon Seller API (остатки по кластерам и FBS) и лист «API Остатки кластеры» (ежедневный снимок). Чем темнее ячейка, тем больше остаток.';
 $('main').innerHTML=html}
init();
</script></body></html>`;
