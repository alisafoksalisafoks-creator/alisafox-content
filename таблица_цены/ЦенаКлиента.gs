/**
 * ЦЕНА КЛИЕНТА: Ozon, Яндекс Маркет, Wildberries
 * Google Apps Script для таблицы цен AlisaFox.
 *
 * Что делает:
 *   - по артикулу площадки в строке берёт из API цену, которую видит покупатель,
 *     и пишет её в колонку «Цена клиента <площадка>»;
 *   - Ozon: сколько реально заплатил покупатель (дневной отчёт о реализации, средняя за день):
 *     вчера, если продаж не было — позавчера и так до 14 дней; совсем без продаж — цена на сайте со скидкой,
 *     а если её нет (товар не в наличии) — ячейка пустая, вашу цену не подставляем. Обновляется только по кнопке;
 *   - ЯМ: цена на витрине; WB: цена со скидкой (кошелёк ВБ не учитывается);
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
    // Голые «Озон/ВБ/ЯМ» не используем: так подписаны колонки цен под общими заголовками.
    OZON: ['Артикул Ozon', 'Артикул Озон', 'SKU Ozon', 'Ozon SKU', 'Артикул OZ', 'Ozon артикул', 'Озон артикул', 'Артикул'],
    YM:   ['Артикул ЯМ', 'Артикул Яндекс Маркет', 'Артикул Яндекс', 'SKU ЯМ', 'SKU на Маркете', 'Артикул YM', 'ЯМ артикул'],
    WB:   ['Артикул WB', 'Артикул ВБ', 'Артикул Wildberries', 'nmID', 'Артикул ВБ (nmID)', 'WB артикул', 'ВБ артикул'],
  },

  // Куда писать цену. Вариант 1: общий заголовок CLIENT_GROUP над колонками (может быть объединённой
  // ячейкой), а под ним подписи площадок из MP_SUBHEADERS. Вариант 2: одна ячейка вида PRICE_HEADERS.
  // Новых колонок скрипт не создаёт.
  CLIENT_GROUP: 'Цена клиента',
  MP_SUBHEADERS: {
    OZON: ['Озон', 'Ozon'],
    YM:   ['ЯМ', 'Яндекс Маркет', 'YM', 'Маркет'],
    WB:   ['ВБ', 'WB', 'Wildberries'],
  },
  PRICE_HEADERS: {
    OZON: 'Цена клиента Ozon',
    YM:   'Цена клиента ЯМ',
    WB:   'Цена клиента WB',
  },

  // Сколько дней назад искать последнюю продажу товара для цены клиента Ozon и WB.
  OZON_FACT_DAYS: 14,

  // СПП WB, %, для товаров без заказов за 14 дней (медиана по заказам на 31.10.2026).
  WB_SPP_DEFAULT: 20,

  // Что писать, если артикул не найден в API (пусто = очистить ячейку).
  NOT_FOUND_TEXT: '',
};

const MP_NAMES = { OZON: 'Ozon', YM: 'Яндекс Маркет', WB: 'Wildberries' };
const KEY_OF = { OZON: 'OZON', YM: 'YM', WB: 'WB' };

// ===================== МЕНЮ =====================

/** Рисует меню. Вызывается триггером при открытии таблицы. */
function addClientPriceMenu() {
  SpreadsheetApp.getUi()
    .createMenu('💰 Цены клиента')
    .addItem('🔄 Ozon: факт цена покупателя', 'cpUpdateOzon')
    .addItem('🔄 Яндекс Маркет: факт цена покупателя', 'cpUpdateYM')
    .addItem('🔄 Wildberries: цена покупателя (с СПП)', 'cpUpdateWB')
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
  // Автообновления нет — только кнопки. Убираем старый дневной триггер, если остался.
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'cpDailyOzon')
    .forEach(t => ScriptApp.deleteTrigger(t));
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
    const found = cpFindCol_(top[r], CP.KEY_HEADERS[KEY_OF[mp]]);
    if (found >= 0) { headerRow = r + 1; keyCol = found + 1; keyHeader = top[r][found]; }
  }
  if (keyCol < 0) {
    throw new Error('Не нашёл колонку с артикулом ' + MP_NAMES[KEY_OF[mp]] + '. Ищу по названиям: ' +
      CP.KEY_HEADERS[KEY_OF[mp]].join(', ') + '. Переименуйте колонку или добавьте её название в CP.KEY_HEADERS в коде.');
  }
  const headers = top[headerRow - 1];

  // 2. Колонка цены: «Цена клиента» → подпись площадки, или одна ячейка «Цена клиента Ozon».
  const priceCol = cpFindPriceCol_(top, headerRow, mp);
  if (priceCol < 1) {
    throw new Error('Не нашёл колонку цены клиента ' + MP_NAMES[mp] + ': нужен общий заголовок «' + CP.CLIENT_GROUP +
      '» и под ним «' + CP.MP_SUBHEADERS[mp][0] + '», или колонка «' + CP.PRICE_HEADERS[mp] + '».');
  }

  // 3. Данные: все строки ниже шапки, включая скрытые и отфильтрованные.
  const dataRows = sh.getLastRow() - headerRow;
  if (dataRows < 1) throw new Error('Под шапкой нет строк.');
  const keys = sh.getRange(headerRow + 1, keyCol, dataRows, 1).getDisplayValues();
  const priceRange = sh.getRange(headerRow + 1, priceCol, dataRows, 1);
  const out = priceRange.getValues();

  // 4. Цены из API: карта «артикул → цена».
  const prices = cpFetch_(mp);
  const note = prices.__note; delete prices.__note;
  const apiCount = Object.keys(prices).length;
  prices.__note = note;
  if (!apiCount) throw new Error('API вернул 0 цен. Проверьте ключ: меню → ⚙️ Настройки → Проверить ключи.');

  // 5. Сопоставление.
  let updated = 0, notFound = 0, empty = 0;
  const notFoundSamples = [];
  for (let i = 0; i < keys.length; i++) {
    const k = cpNormKey_(keys[i][0]);
    if (!k) { empty++; continue; }        // пустую строку не трогаем
    const p = prices[k];
    if (p != null) { out[i][0] = p; updated++; }
    else if (mp === 'YM') { notFound++; }  // ЯМ: без заказов — значение в ячейке не трогаем
    else {
      out[i][0] = CP.NOT_FOUND_TEXT; notFound++;
      if (notFoundSamples.length < 10) notFoundSamples.push(keys[i][0]);
    }
  }
  priceRange.setValues(out); // setValues пишет и в скрытые/отфильтрованные строки
  // Убираем старые примечания других скриптов с ячеек, которые мы переписали.
  const notes = priceRange.getNotes();
  for (let i = 0; i < keys.length; i++) {
    const k = cpNormKey_(keys[i][0]);
    if (k && (prices[k] != null || mp !== 'YM')) notes[i][0] = '';
  }
  priceRange.setNotes(notes);
  priceRange.setNumberFormat('#,##0 ₽');
  sh.getRange(headerRow, priceCol).setNote(
    MP_NAMES[mp] + (prices.__note ? ' — ' + prices.__note : '') + ': обновлено ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'dd.MM.yyyy HH:mm') +
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

/** Номер колонки (с 1) цены клиента площадки mp или -1. */
function cpFindPriceCol_(top, headerRow, mp) {
  const hdr = top[headerRow - 1];
  const direct = cpFindCol_(hdr, [CP.PRICE_HEADERS[mp]]);
  if (direct >= 0) return direct + 1;
  const g = cpNormHeader_(CP.CLIENT_GROUP);
  const subs = CP.MP_SUBHEADERS[mp].map(cpNormHeader_);
  for (let r = 0; r < top.length; r++) {
    const row = top[r];
    for (let c = 0; c < row.length; c++) {
      if (cpNormHeader_(row[c]) !== g) continue;
      // Объединённая ячейка хранит текст только в первой колонке: группа идёт до следующего заголовка в этой строке.
      let end = c + 1;
      while (end < row.length && !String(row[end]).trim()) end++;
      for (let rr = r; rr < Math.min(top.length, r + 3); rr++) {
        for (let cc = c; cc < end; cc++) {
          if (subs.indexOf(cpNormHeader_(top[rr][cc])) >= 0) return cc + 1;
        }
      }
    }
  }
  return -1;
}

function cpNormKey_(v) {
  return String(v == null ? '' : v).trim().replace(/\s+/g, '').replace(/^'/, '').toLowerCase();
}

function cpAddPrice_(map, key, price) {
  const k = cpNormKey_(key);
  const p = Number(price);
  if (k && isFinite(p) && p > 0) map[k] = Math.round(p); // цена клиента — целым числом
}

function cpFetch_(mp) {
  if (mp === 'OZON') return cpOzonClientPrices_();
  if (mp === 'YM') return cpYMFactPrices_();
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
// Цена на сайте = website_prices.price из акций (/v2/actions/products и /candidates).
// Это цена с Ozon Картой для FBO, именно её видит покупатель (проверено 31.10: OZ066 239 → 210 ₽).
// Товар не в акциях — цена продавца price из /v5/product/info/prices.
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
  const offers = {};
  let cursor = '', guard = 0;
  do {
    const r = cpHttp_('https://api-seller.ozon.ru/v5/product/info/prices', {
      method: 'post', headers: h,
      body: { cursor: cursor, filter: { visibility: 'ALL' }, limit: 1000 },
    });
    (r.items || []).forEach(it => {
      if (it.product_id) { byProductId[it.product_id] = Number((it.price || {}).price); offers[it.product_id] = it.offer_id; }
    });
    cursor = r.cursor || '';
    if (!(r.items || []).length) break;
  } while (cursor && ++guard < 200);

  // Цена на сайте из акций: участник акции важнее кандидата.
  const site = cpOzonWebsitePrices_(h);
  Object.keys(byProductId).forEach(pid => {
    const s = site[pid];
    // Только реальная цена на сайте. Если Озон отдаёт её равной вашей цене (товара нет в наличии,
    // скидки нет) или товара нет в акциях — цену клиента не знаем, ячейку оставляем пустой.
    const w = s ? (s.products || s.candidates) : null;
    const v = (w && w !== byProductId[pid]) ? w : null;
    byProductId[pid] = v;
    cpAddPrice_(map, pid, v);
    cpAddPrice_(map, offers[pid], v);
  });

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

/** product_id → { products: мин. цена на сайте как участника акций, candidates: как кандидата }. */
function cpOzonWebsitePrices_(h) {
  const out = {};
  const acts = (cpHttp_('https://api-seller.ozon.ru/v1/actions', { headers: h }).result) || [];
  acts.forEach(a => {
    ['products', 'candidates'].forEach(ep => {
      let last = '', guard = 0;
      do {
        const r = cpHttp_('https://api-seller.ozon.ru/v2/actions/' + ep, {
          method: 'post', headers: h, body: { action_id: a.id, limit: 100, last_id: last },
        });
        const list = r.products || [];
        list.forEach(p => {
          const w = Number(p.website_prices && p.website_prices.price && p.website_prices.price.amount);
          if (!(w > 0)) return;
          const o = out[p.id] || (out[p.id] = {});
          o[ep] = o[ep] ? Math.min(o[ep], w) : w;
        });
        last = r.last_id || '';
        if (!list.length) break;
      } while (last && ++guard < 500);
    });
  });
  return out;
}

// ===================== OZON: ФАКТ ЦЕНЫ ПОКУПАТЕЛЯ ЗА ВЧЕРА =====================
// Дневной отчёт о реализации /v1/finance/realization/by-day:
// delivery_commission.price_per_instance — сколько заплатил покупатель за штуку
// (скидки за счёт Озона он компенсирует продавцу отдельно, поле bonus).
// В отчёт попадают выкупы по дате получения. Несколько продаж за день — средняя цена.

function cpDaysAgo_(n) {
  const d = new Date(Date.now() - n * 24 * 3600 * 1000);
  const f = x => Utilities.formatDate(d, 'Europe/Moscow', x);
  return { day: Number(f('d')), month: Number(f('M')), year: Number(f('yyyy')), label: f('dd.MM.yyyy') };
}

/**
 * Цена клиента Ozon = последняя фактическая цена покупателя:
 * вчера; если вчера товар не продавался — позавчера, и так до CP.OZON_FACT_DAYS дней назад.
 * Не было продаж за весь период — текущая цена на сайте.
 */
function cpOzonClientPrices_() {
  const site = cpOzonPrices_();
  const fact = {};
  let daysOk = 0, first = '', last = '';
  for (let n = 1; n <= CP.OZON_FACT_DAYS; n++) {
    const y = cpDaysAgo_(n);
    let day;
    try { day = cpOzonFactPrices_(y); }
    catch (e) { if (e.code === 404) continue; throw e; } // день ещё не сформирован
    daysOk++; if (!first) first = y.label; last = y.label;
    Object.keys(day).forEach(k => { if (!(k in fact)) fact[k] = day[k]; });
  }
  const map = Object.assign({}, site, fact);
  const factGoods = Object.keys(fact).filter(k => !/^\d+$/.test(k)).length;
  map.__note = daysOk
    ? 'факт цена покупателя, последний день с продажами за ' + last + '–' + first +
      ' (' + factGoods + ' артикулов); без продаж за период — цена на сайте'
    : 'отчёты о реализации не получены, везде цена на сайте';
  return map;
}

function cpOzonFactPrices_(y) {
  const h = cpOzonHeaders_();
  const r = cpHttp_('https://api-seller.ozon.ru/v1/finance/realization/by-day', {
    method: 'post', headers: h, body: { day: y.day, month: y.month, year: y.year },
  });
  const sum = {}, qty = {}, skuOf = {};
  (r.rows || []).forEach(row => {
    const dc = row.delivery_commission;
    const it = row.item || {};
    if (!dc || !it.offer_id || !(dc.quantity > 0) || !(dc.price_per_instance > 0)) return;
    sum[it.offer_id] = (sum[it.offer_id] || 0) + dc.price_per_instance * dc.quantity;
    qty[it.offer_id] = (qty[it.offer_id] || 0) + dc.quantity;
    if (it.sku) skuOf[it.offer_id] = it.sku;
  });
  const map = {};
  Object.keys(sum).forEach(o => {
    const avg = sum[o] / qty[o];
    cpAddPrice_(map, o, avg);
    if (skuOf[o]) cpAddPrice_(map, skuOf[o], avg);
  });
  // product_id и все SKU товара — чтобы строку нашло по любому артикулу Озона.
  const offers = Object.keys(sum);
  for (let i = 0; i < offers.length; i += 1000) {
    let info;
    try {
      info = cpHttp_('https://api-seller.ozon.ru/v3/product/info/list', {
        method: 'post', headers: h, body: { offer_id: offers.slice(i, i + 1000) },
      });
    } catch (e) { break; }
    (info.items || []).forEach(it => {
      const avg = map[cpNormKey_(it.offer_id)];
      if (!avg) return;
      cpAddPrice_(map, it.id, avg);
      (it.sources || []).forEach(s => s.sku && cpAddPrice_(map, s.sku, avg));
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
        // Нет цены в магазинах — берём базовую цену кабинета.
        const bp = m.offer && m.offer.basicPrice && m.offer.basicPrice.value;
        if (id && !byOffer[id] && bp) { byOffer[id] = bp; cpAddPrice_(map, id, bp); }
        if (id && sku && byOffer[id]) cpAddPrice_(map, sku, byOffer[id]);
      });
      token = res.paging && res.paging.nextPageToken;
    } while (token && ++guard < 1000);
  });
  return map;
}

/**
 * Цена клиента ЯМ: сколько заплатил покупатель (цена BUYER в заказе — уже со скидками и субсидиями Маркета),
 * средняя по штукам за последний день с заказами за CP.OZON_FACT_DAYS дней. Отменённые заказы
 * и оплату баллами почти целиком (≤ 10 ₽) не считаем. Нет заказов — ячейку не трогаем
 * (там остаётся цена на витрине из отчёта «Цены»).
 */
function cpYMFactPrices_() {
  const h = cpYMHeaders_();
  const camps = cpYMCampaigns_(h);
  if (!camps.length) throw new Error('По ключу ЯМ не найдено ни одного магазина.');
  const from = cpDaysAgo_(CP.OZON_FACT_DAYS);
  const pad = n => (n < 10 ? '0' : '') + n;
  const dateFrom = from.year + '-' + pad(from.month) + '-' + pad(from.day);
  const dateTo = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  const agg = {}; // offerId → {day, sum, qty, msku}
  camps.forEach(c => {
    let token = '', guard = 0;
    do {
      const q = '?limit=200' + (token ? '&page_token=' + encodeURIComponent(token) : '');
      const r = cpHttp_(YM_BASE + '/v2/campaigns/' + c.id + '/stats/orders' + q,
        { method: 'post', headers: h, body: { dateFrom: dateFrom, dateTo: dateTo } });
      const res = r.result || {};
      (res.orders || []).forEach(o => {
        if (/^CANCELLED/.test(o.status || '')) return;
        const day = o.creationDate || '';
        (o.items || []).forEach(it => {
          const buyer = (it.prices || []).find(p => p.type === 'BUYER');
          const v = buyer && buyer.costPerItem, n = it.count || 1;
          if (!it.shopSku || !(v > 10)) return;
          const a = agg[it.shopSku];
          if (!a || day > a.day) agg[it.shopSku] = { day: day, sum: v * n, qty: n, msku: it.marketSku };
          else if (day === a.day) { a.sum += v * n; a.qty += n; }
        });
      });
      token = res.paging && res.paging.nextPageToken;
    } while (token && ++guard < 200);
  });
  const map = {};
  Object.keys(agg).forEach(id => {
    const a = agg[id], avg = a.sum / a.qty;
    cpAddPrice_(map, id, avg);
    if (a.msku) cpAddPrice_(map, a.msku, avg);
  });
  map.__note = 'факт: сколько заплатил покупатель, последний день с заказами с ' + dateFrom +
    ' (' + Object.keys(agg).length + ' товаров); без заказов ячейка не меняется';
  return map;
}

// ===================== WILDBERRIES =====================
// Цена клиента WB:
//   1) был заказ за 14 дней — сколько заплатил покупатель (finishedPrice из заказов, с СПП),
//      средняя за последний день с заказами;
//   2) заказов не было — цена со скидкой (discountedPrice) минус СПП CP.WB_SPP_DEFAULT %.
// Кошелёк ВБ не учитывается. Ключи строки: nmID и артикул продавца (vendorCode).

function cpWBPrices_() {
  const token = cpProp_('WB_API_KEY');
  if (!token) throw new Error('Не задан токен Wildberries: меню → ⚙️ Настройки.');
  const h = { Authorization: token };
  const map = {};
  const k = 1 - CP.WB_SPP_DEFAULT / 100;
  let offset = 0, guard = 0;
  while (guard++ < 500) {
    const r = cpHttp_('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000&offset=' + offset, { headers: h });
    const list = (r.data && r.data.listGoods) || [];
    list.forEach(g => {
      const ps = (g.sizes || []).map(s => Number(s.discountedPrice)).filter(x => x > 0);
      const p = ps.length ? Math.min.apply(null, ps) * k : null;
      if (p) { cpAddPrice_(map, g.nmID, p); cpAddPrice_(map, g.vendorCode, p); }
    });
    if (list.length < 1000) break;
    offset += 1000;
    Utilities.sleep(700); // лимит WB: не чаще 10 запросов за 6 сек
  }

  // Факт из заказов за 14 дней (токен должен иметь доступ к «Статистике»).
  let orders = [];
  try {
    const from = Utilities.formatDate(new Date(Date.now() - CP.OZON_FACT_DAYS * 864e5), 'Europe/Moscow', 'yyyy-MM-dd');
    orders = cpHttp_('https://statistics-api.wildberries.ru/api/v1/supplier/orders?dateFrom=' + from, { headers: h }) || [];
  } catch (e) { orders = []; } // нет доступа к статистике — остаётся оценка по СПП
  const byKey = {};
  orders.forEach(o => {
    if (!(o.finishedPrice > 0)) return;
    const day = String(o.date).slice(0, 10);
    [o.supplierArticle, o.nmId].forEach(key => {
      const kk = cpNormKey_(key); if (!kk) return;
      const d = byKey[kk] || (byKey[kk] = {});
      (d[day] || (d[day] = [])).push(Number(o.finishedPrice));
    });
  });
  Object.keys(byKey).forEach(kk => {
    const days = Object.keys(byKey[kk]).sort();
    const v = byKey[kk][days[days.length - 1]];
    map[kk] = Math.round(v.reduce((a, b) => a + b, 0) / v.length);
  });
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
      const oz = cpOzonPrices_();
      out = { offer_id: art, 'цена на сайте (как пишет скрипт)': oz[cpNormKey_(art)] || 'не найдено',
        prices: cpHttp_('https://api-seller.ozon.ru/v5/product/info/prices', {
          method: 'post', headers: cpOzonHeaders_(),
          body: { cursor: '', filter: { offer_id: [art], visibility: 'ALL' }, limit: 10 },
        }) };
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
