/**
 * ЦЕНА КЛИЕНТА: Ozon, Яндекс Маркет, Wildberries
 * Google Apps Script для таблицы цен AlisaFox.
 *
 * Что делает:
 *   - по артикулу площадки в строке берёт из API цену, которую видит покупатель,
 *     и пишет её в колонку «Цена клиента <площадка>»;
 *   - Ozon: цена на сайте; ЯМ: цена на витрине; WB: цена со скидкой (кошелёк ВБ не учитывается);
 *   - колонки ищутся по НАЗВАНИЮ в шапке, а не по букве: удаление и перестановка столбцов не ломают скрипт;
 *   - пишет во ВСЕ строки, в том числе скрытые и отфильтрованные;
 *   - меню «💰 Цены клиента» с отдельной кнопкой на каждую площадку.
 *
 * Ключи API лежат в «Свойствах скрипта», а не в коде.
 * Меню подключается через отдельный триггер, поэтому не конфликтует с уже существующим onOpen.
 */

// ===================== НАСТРОЙКИ =====================

const CP = {
  // Имя листа. Пусто = лист с gid=0 (первый, как в ссылке), иначе активный.
  SHEET_NAME: '',

  // В скольких верхних строках искать шапку.
  HEADER_SCAN_ROWS: 15,

  // Названия колонок с артикулом площадки. Скрипт возьмёт первую найденную.
  // Регистр, ё/е, пробелы и знаки препинания не важны.
  KEY_HEADERS: {
    OZON: ['Артикул Ozon', 'Артикул Озон', 'SKU Ozon', 'Ozon SKU', 'Артикул OZ', 'Ozon артикул', 'Озон артикул', 'Озон', 'Ozon'],
    YM:   ['Артикул ЯМ', 'Артикул Яндекс Маркет', 'Артикул Яндекс', 'SKU ЯМ', 'SKU на Маркете', 'Артикул YM', 'ЯМ артикул', 'ЯМ', 'Яндекс Маркет'],
    WB:   ['Артикул WB', 'Артикул ВБ', 'Артикул Wildberries', 'nmID', 'Артикул ВБ (nmID)', 'WB артикул', 'ВБ артикул', 'ВБ', 'WB'],
  },

  // Куда писать цену. Если колонки нет, скрипт создаст её справа от последней колонки шапки.
  PRICE_HEADERS: {
    OZON: 'Цена клиента Ozon',
    YM:   'Цена клиента ЯМ',
    WB:   'Цена клиента WB',
  },

  // Что писать, если артикул не найден в API (пусто = очистить ячейку).
  NOT_FOUND_TEXT: '',
};

const MP_NAMES = { OZON: 'Ozon', YM: 'Яндекс Маркет', WB: 'Wildberries' };

// ===================== МЕНЮ =====================

/** Рисует меню. Вызывается триггером при открытии таблицы. */
function addClientPriceMenu() {
  SpreadsheetApp.getUi()
    .createMenu('💰 Цены клиента')
    .addItem('🔄 Ozon: цена на сайте', 'cpUpdateOzon')
    .addItem('🔄 Яндекс Маркет: цена на витрине', 'cpUpdateYM')
    .addItem('🔄 Wildberries: цена со скидкой', 'cpUpdateWB')
    .addSeparator()
    .addItem('🔄 Все три площадки', 'cpUpdateAll')
    .addSeparator()
    .addSubMenu(SpreadsheetApp.getUi().createMenu('⚙️ Настройки')
      .addItem('Ключи Ozon (Client-Id и Api-Key)', 'cpSetOzonKeys')
      .addItem('Ключ Яндекс Маркета (Api-Key)', 'cpSetYMKey')
      .addItem('Ключ Wildberries (токен «Цены и скидки»)', 'cpSetWBKey')
      .addItem('Проверить ключи', 'cpCheckKeys')
      .addItem('Диагностика: ответ API по одному артикулу', 'cpDiagnose'))
    .addToUi();
}

/**
 * ЗАПУСТИТЬ ОДИН РАЗ из редактора (кнопка ▶ «Выполнить»).
 * Ставит триггер, который добавляет меню при каждом открытии таблицы,
 * и сразу рисует меню. Существующий onOpen других скриптов не трогает.
 */
function cpInstall() {
  const ss = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'addClientPriceMenu')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('addClientPriceMenu').forSpreadsheet(ss).onOpen().create();
  addClientPriceMenu();
  ss.toast('Меню «💰 Цены клиента» установлено. Теперь задайте ключи: меню → ⚙️ Настройки.', 'Цены клиента', 8);
}

// ===================== КНОПКИ =====================

function cpUpdateOzon() { cpRun_('OZON'); }
function cpUpdateYM()   { cpRun_('YM'); }
function cpUpdateWB()   { cpRun_('WB'); }
function cpUpdateAll()  { ['OZON', 'YM', 'WB'].forEach(mp => cpRun_(mp, true)); }

function cpRun_(mp, silent) {
  const ss = SpreadsheetApp.getActive();
  ss.toast('Загружаю цены ' + MP_NAMES[mp] + ' из API…', 'Цены клиента', 30);
  try {
    const res = cpUpdate_(mp);
    const msg = MP_NAMES[mp] + ': обновлено ' + res.updated + ' строк' +
      (res.notFound ? ', не найдено в API ' + res.notFound : '') +
      (res.empty ? ', без артикула ' + res.empty : '') +
      '. Цен в API: ' + res.apiCount + '.';
    ss.toast(msg, 'Цены клиента ✅', 10);
    if (res.notFound && !silent) {
      SpreadsheetApp.getUi().alert(msg + '\n\nПримеры ненайденных артикулов: ' +
        res.notFoundSamples.join(', ') +
        '\nПроверьте, что в колонке «' + res.keyHeader + '» артикулы именно ' + MP_NAMES[mp] + '.');
    }
  } catch (e) {
    ss.toast(String(e.message || e), 'Цены клиента ❌ ' + MP_NAMES[mp], 15);
    if (!silent) SpreadsheetApp.getUi().alert(MP_NAMES[mp] + ': ' + (e.message || e));
  }
}

// ===================== ЯДРО: ЗАПИСЬ В ТАБЛИЦУ =====================

function cpUpdate_(mp) {
  const sh = cpSheet_();
  const lastRow = sh.getMaxRows();
  const lastCol = sh.getLastColumn();
  if (lastCol < 1) throw new Error('Лист пустой.');

  // 1. Шапка: ищем строку, где есть колонка артикула этой площадки.
  const scan = Math.min(CP.HEADER_SCAN_ROWS, lastRow);
  const top = sh.getRange(1, 1, scan, lastCol).getDisplayValues();
  let headerRow = -1, keyCol = -1, keyHeader = '';
  for (let r = 0; r < top.length && keyCol < 0; r++) {
    const found = cpFindCol_(top[r], CP.KEY_HEADERS[mp]);
    if (found >= 0) { headerRow = r + 1; keyCol = found + 1; keyHeader = top[r][found]; }
  }
  if (keyCol < 0) {
    throw new Error('Не нашёл колонку с артикулом ' + MP_NAMES[mp] + '. Ищу по названиям: ' +
      CP.KEY_HEADERS[mp].join(', ') + '. Переименуйте колонку или добавьте её название в CP.KEY_HEADERS в коде.');
  }
  const headers = top[headerRow - 1];

  // 2. Колонка цены: находим или создаём.
  let priceCol = cpFindCol_(headers, [CP.PRICE_HEADERS[mp]]) + 1;
  if (priceCol < 1) {
    priceCol = lastCol + 1;
    if (priceCol > sh.getMaxColumns()) sh.insertColumnsAfter(sh.getMaxColumns(), 1);
    sh.getRange(headerRow, priceCol).setValue(CP.PRICE_HEADERS[mp]).setFontWeight('bold');
  }

  // 3. Данные: все строки ниже шапки, включая скрытые и отфильтрованные.
  const dataRows = sh.getLastRow() - headerRow;
  if (dataRows < 1) throw new Error('Под шапкой нет строк.');
  const keys = sh.getRange(headerRow + 1, keyCol, dataRows, 1).getDisplayValues();
  const priceRange = sh.getRange(headerRow + 1, priceCol, dataRows, 1);
  const out = priceRange.getValues();

  // 4. Цены из API: карта «артикул → цена».
  const prices = cpFetch_(mp);
  const apiCount = Object.keys(prices).length;
  if (!apiCount) throw new Error('API вернул 0 цен. Проверьте ключ: меню → ⚙️ Настройки → Проверить ключи.');

  // 5. Сопоставление.
  let updated = 0, notFound = 0, empty = 0;
  const notFoundSamples = [];
  for (let i = 0; i < keys.length; i++) {
    const k = cpNormKey_(keys[i][0]);
    if (!k) { empty++; continue; }        // пустую строку не трогаем
    const p = prices[k];
    if (p != null) { out[i][0] = p; updated++; }
    else {
      out[i][0] = CP.NOT_FOUND_TEXT; notFound++;
      if (notFoundSamples.length < 10) notFoundSamples.push(keys[i][0]);
    }
  }
  priceRange.setValues(out); // setValues пишет и в скрытые/отфильтрованные строки
  priceRange.setNumberFormat('#,##0 ₽');
  sh.getRange(headerRow, priceCol).setNote(
    MP_NAMES[mp] + ': обновлено ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'dd.MM.yyyy HH:mm') +
    ' МСК, строк ' + updated + ', не найдено ' + notFound);

  return { updated, notFound, empty, apiCount, notFoundSamples, keyHeader };
}

function cpSheet_() {
  const ss = SpreadsheetApp.getActive();
  if (CP.SHEET_NAME) {
    const s = ss.getSheetByName(CP.SHEET_NAME);
    if (!s) throw new Error('Нет листа «' + CP.SHEET_NAME + '».');
    return s;
  }
  return ss.getSheets().find(s => s.getSheetId() === 0) || ss.getActiveSheet();
}

function cpNormHeader_(s) {
  return String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/[\s\.\,\-_:;()«»"'\/\\]+/g, '');
}

function cpFindCol_(row, candidates) {
  const norm = row.map(cpNormHeader_);
  for (const c of candidates) {            // приоритет — порядок в списке
    const i = norm.indexOf(cpNormHeader_(c));
    if (i >= 0) return i;
  }
  return -1;
}

function cpNormKey_(v) {
  return String(v == null ? '' : v).trim().replace(/\s+/g, '').replace(/^'/, '').toLowerCase();
}

function cpAddPrice_(map, key, price) {
  const k = cpNormKey_(key);
  const p = Number(price);
  if (k && isFinite(p) && p > 0) map[k] = Math.round(p * 100) / 100;
}

function cpFetch_(mp) {
  if (mp === 'OZON') return cpOzonPrices_();
  if (mp === 'YM') return cpYMPrices_();
  if (mp === 'WB') return cpWBPrices_();
  throw new Error('Неизвестная площадка ' + mp);
}

// ===================== HTTP =====================

function cpHttp_(url, opt) {
  opt = opt || {};
  const params = {
    method: opt.method || 'get',
    headers: opt.headers || {},
    muteHttpExceptions: true,
  };
  if (opt.body !== undefined) {
    params.contentType = 'application/json';
    params.payload = JSON.stringify(opt.body);
  }
  for (let attempt = 1; attempt <= 5; attempt++) {
    const r = UrlFetchApp.fetch(url, params);
    const code = r.getResponseCode();
    if (code === 429 || code >= 500) { Utilities.sleep(1500 * attempt); continue; }
    const text = r.getContentText();
    if (code >= 400) {
      const e = new Error('HTTP ' + code + ' ' + url.replace(/\?.*/, '') + ': ' + text.slice(0, 300));
      e.code = code;
      throw e;
    }
    return text ? JSON.parse(text) : {};
  }
  throw new Error('API не отвечает (много попыток): ' + url.replace(/\?.*/, ''));
}

function cpProp_(name) {
  return PropertiesService.getScriptProperties().getProperty(name) || '';
}

// ===================== OZON =====================
// Цена на сайте: price.marketing_price из /v5/product/info/prices (цена на витрине с акциями, без Ozon Карты).
// Если Озон вернул его пустым — берём marketing_seller_price, затем price.
// Ключи строки: offer_id (артикул продавца), product_id и SKU Озона — подойдёт любой из них.

function cpOzonHeaders_() {
  const id = cpProp_('OZON_CLIENT_ID'), key = cpProp_('OZON_API_KEY');
  if (!id || !key) throw new Error('Не заданы ключи Ozon: меню → ⚙️ Настройки → Ключи Ozon.');
  return { 'Client-Id': id, 'Api-Key': key };
}

function cpOzonPrices_() {
  const h = cpOzonHeaders_();
  const map = {};
  const byProductId = {};
  let cursor = '', guard = 0;
  do {
    const r = cpHttp_('https://api-seller.ozon.ru/v5/product/info/prices', {
      method: 'post', headers: h,
      body: { cursor: cursor, filter: { visibility: 'ALL' }, limit: 1000 },
    });
    (r.items || []).forEach(it => {
      const p = it.price || {};
      const site = Number(p.marketing_price) || Number(p.marketing_seller_price) || Number(p.price);
      cpAddPrice_(map, it.offer_id, site);
      cpAddPrice_(map, it.product_id, site);
      if (it.product_id) byProductId[it.product_id] = site;
    });
    cursor = r.cursor || '';
    if (!(r.items || []).length) break;
  } while (cursor && ++guard < 200);

  // SKU Озона по product_id
  const ids = Object.keys(byProductId);
  for (let i = 0; i < ids.length; i += 1000) {
    const chunk = ids.slice(i, i + 1000);
    let r;
    try {
      r = cpHttp_('https://api-seller.ozon.ru/v3/product/info/list', {
        method: 'post', headers: h, body: { product_id: chunk },
      });
    } catch (e) { break; } // без SKU работаем по offer_id/product_id
    (r.items || []).forEach(it => {
      const site = byProductId[it.id];
      if (it.sku) cpAddPrice_(map, it.sku, site);
      (it.sources || []).forEach(s => s.sku && cpAddPrice_(map, s.sku, site));
    });
  }
  return map;
}

// ===================== ЯНДЕКС МАРКЕТ =====================
// Цена на витрине: цена товара в магазине (campaign offer-prices), по всем магазинам кабинета.
// Ключи строки: ваш SKU (offerId) и SKU на Маркете (marketSku).

const YM_BASE = 'https://api.partner.market.yandex.ru';

function cpYMHeaders_() {
  const key = cpProp_('YM_API_KEY');
  if (!key) throw new Error('Не задан ключ Яндекс Маркета: меню → ⚙️ Настройки.');
  return { 'Api-Key': key };
}

function cpYMCampaigns_(h) {
  const r = cpHttp_(YM_BASE + '/campaigns?pageSize=50', { headers: h });
  return (r.campaigns || []).map(c => ({ id: c.id, businessId: c.business && c.business.id }));
}

function cpYMPrices_() {
  const h = cpYMHeaders_();
  const camps = cpYMCampaigns_(h);
  if (!camps.length) throw new Error('По ключу ЯМ не найдено ни одного магазина.');
  const map = {};
  const byOffer = {};

  camps.forEach(c => {
    let token = '', guard = 0;
    do {
      const q = '?limit=500' + (token ? '&page_token=' + encodeURIComponent(token) : '');
      let r;
      try {
        r = cpHttp_(YM_BASE + '/v2/campaigns/' + c.id + '/offer-prices' + q, { method: 'post', headers: h, body: {} });
      } catch (e) {
        if (e.code !== 404) throw e;
        r = cpHttp_(YM_BASE + '/campaigns/' + c.id + '/offer-prices' + q, { method: 'post', headers: h, body: {} });
      }
      const res = r.result || {};
      (res.offers || []).forEach(o => {
        const v = o.price && (o.price.value != null ? o.price.value : o.price.price);
        if (v && !(o.offerId in byOffer)) { byOffer[o.offerId] = v; cpAddPrice_(map, o.offerId, v); }
      });
      token = res.paging && res.paging.nextPageToken;
    } while (token && ++guard < 500);
  });

  // SKU на Маркете → offerId
  const businessIds = [...new Set(camps.map(c => c.businessId).filter(Boolean))];
  businessIds.forEach(b => {
    let token = '', guard = 0;
    do {
      const q = '?limit=200' + (token ? '&page_token=' + encodeURIComponent(token) : '');
      let r;
      try {
        r = cpHttp_(YM_BASE + '/v2/businesses/' + b + '/offer-mappings' + q, { method: 'post', headers: h, body: {} });
      } catch (e) {
        if (e.code !== 404) return;
        r = cpHttp_(YM_BASE + '/businesses/' + b + '/offer-mappings' + q, { method: 'post', headers: h, body: {} });
      }
      const res = r.result || {};
      (res.offerMappings || []).forEach(m => {
        const id = m.offer && m.offer.offerId;
        const sku = m.mapping && m.mapping.marketSku;
        if (id && sku && byOffer[id]) cpAddPrice_(map, sku, byOffer[id]);
      });
      token = res.paging && res.paging.nextPageToken;
    } while (token && ++guard < 1000);
  });
  return map;
}

// ===================== WILDBERRIES =====================
// Цена со скидкой: discountedPrice из API «Цены и скидки» (без СПП и без кошелька ВБ).
// Если у размеров разные цены — берётся минимальная.
// Ключи строки: артикул ВБ (nmID) и артикул продавца (vendorCode).

function cpWBPrices_() {
  const token = cpProp_('WB_API_KEY');
  if (!token) throw new Error('Не задан токен Wildberries: меню → ⚙️ Настройки.');
  const h = { Authorization: token };
  const map = {};
  let offset = 0, guard = 0;
  while (guard++ < 500) {
    const r = cpHttp_('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000&offset=' + offset, { headers: h });
    const list = (r.data && r.data.listGoods) || [];
    list.forEach(g => {
      const ps = (g.sizes || []).map(s => Number(s.discountedPrice)).filter(x => x > 0);
      const p = ps.length ? Math.min.apply(null, ps) : null;
      if (p) { cpAddPrice_(map, g.nmID, p); cpAddPrice_(map, g.vendorCode, p); }
    });
    if (list.length < 1000) break;
    offset += 1000;
    Utilities.sleep(700); // лимит WB: не чаще 10 запросов за 6 сек
  }
  return map;
}

// ===================== НАСТРОЙКИ КЛЮЧЕЙ =====================

function cpAsk_(title, prompt, prop) {
  const ui = SpreadsheetApp.getUi();
  const r = ui.prompt(title, prompt + '\n(Оставьте пустым, чтобы не менять.)', ui.ButtonSet.OK_CANCEL);
  if (r.getSelectedButton() !== ui.Button.OK) return false;
  const v = r.getResponseText().trim();
  if (v) PropertiesService.getScriptProperties().setProperty(prop, v);
  return true;
}

function cpSetOzonKeys() {
  if (cpAsk_('Ozon', 'Client-Id (число из Настройки → Seller API):', 'OZON_CLIENT_ID'))
    cpAsk_('Ozon', 'Api-Key (ключ с правом чтения товаров и цен):', 'OZON_API_KEY');
}
function cpSetYMKey() { cpAsk_('Яндекс Маркет', 'Api-Key (токен с доступом к ценам и каталогу):', 'YM_API_KEY'); }
function cpSetWBKey() { cpAsk_('Wildberries', 'Токен API с категорией «Цены и скидки»:', 'WB_API_KEY'); }

function cpCheckKeys() {
  const lines = [];
  try { const m = cpOzonPrices_(); lines.push('Ozon: ✅ цен получено ' + Object.keys(m).length); }
  catch (e) { lines.push('Ozon: ❌ ' + e.message); }
  try { const m = cpYMPrices_(); lines.push('ЯМ: ✅ цен получено ' + Object.keys(m).length); }
  catch (e) { lines.push('ЯМ: ❌ ' + e.message); }
  try { const m = cpWBPrices_(); lines.push('WB: ✅ цен получено ' + Object.keys(m).length); }
  catch (e) { lines.push('WB: ❌ ' + e.message); }
  SpreadsheetApp.getUi().alert(lines.join('\n\n') + '\n\n(Число — это ключи: у одного товара их может быть 2–3.)');
}

/**
 * Показывает сырой ответ API по одному артикулу — чтобы сверить цену с витриной.
 * Ответ можно скопировать и прислать Claude.
 */
function cpDiagnose() {
  const ui = SpreadsheetApp.getUi();
  const mpR = ui.prompt('Диагностика', 'Площадка: oz, ym или wb', ui.ButtonSet.OK_CANCEL);
  if (mpR.getSelectedButton() !== ui.Button.OK) return;
  const mp = mpR.getResponseText().trim().toLowerCase();
  const artR = ui.prompt('Диагностика', 'Артикул продавца (offer_id / offerId / vendorCode) или nmID для WB:', ui.ButtonSet.OK_CANCEL);
  if (artR.getSelectedButton() !== ui.Button.OK) return;
  const art = artR.getResponseText().trim();
  let out;
  try {
    if (mp.startsWith('o')) {
      out = cpHttp_('https://api-seller.ozon.ru/v5/product/info/prices', {
        method: 'post', headers: cpOzonHeaders_(),
        body: { cursor: '', filter: { offer_id: [art], visibility: 'ALL' }, limit: 10 },
      });
    } else if (mp.startsWith('y')) {
      const h = cpYMHeaders_();
      const c = cpYMCampaigns_(h)[0];
      try {
        out = cpHttp_(YM_BASE + '/v2/campaigns/' + c.id + '/offer-prices', { method: 'post', headers: h, body: { offerIds: [art] } });
      } catch (e) {
        out = cpHttp_(YM_BASE + '/campaigns/' + c.id + '/offer-prices', { method: 'post', headers: h, body: { offerIds: [art] } });
      }
    } else {
      const h = { Authorization: cpProp_('WB_API_KEY') };
      const isNm = /^\d+$/.test(art);
      out = cpHttp_('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=10&filterNmID=' +
        (isNm ? art : '') + (isNm ? '' : '&offset=0'), { headers: h });
    }
  } catch (e) { out = { error: e.message }; }
  const text = JSON.stringify(out, null, 1).slice(0, 4000);
  Logger.log(text);
  const html = HtmlService.createHtmlOutput('<textarea style="width:100%;height:95%;font-family:monospace">' +
    text.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</textarea>').setWidth(700).setHeight(500);
  ui.showModalDialog(html, 'Ответ API (' + mp + ', ' + art + ')');
}
