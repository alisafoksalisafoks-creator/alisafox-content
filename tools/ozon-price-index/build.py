import json,sys,openpyxl,datetime
from openpyxl.styles import Font,PatternFill,Alignment
from openpyxl.utils import get_column_letter as L
from openpyxl.formatting.rule import FormulaRule
D=sys.argv[1]; COST=sys.argv[2]; OUT=sys.argv[3]
p=json.load(open(D+'/prices.json')); info={x['id']:x for x in json.load(open(D+'/info.json'))}
cw=openpyxl.load_workbook(COST,data_only=True).active
cost={}
for r in cw.iter_rows(min_row=3,values_only=True):
    if not r[0]:continue
    v=[c for c in (r[2],r[5],r[8]) if c not in (None,'')]
    c=v[-1] if v else 0
    cost[str(r[0]).strip()]=float(c or 0)
CN={'SUPER':'Супервыгодная','GREEN':'Выгодная','YELLOW':'Умеренная','RED':'Невыгодная','WITHOUT_INDEX':'Нет индекса'}
ORD={'RED':0,'YELLOW':1,'GREEN':2,'SUPER':3,'WITHOUT_INDEX':4}
TAX,TGT,MM,LK=0.07,1.0,0.10,0.5
rows=[]
for x in p:
    i=info.get(x['product_id'],{}); pi=x['price_indexes']; pr=x['price']; cm=x['commissions']
    st={s['source']:s['present'] for s in (i.get('stocks') or {}).get('stocks',[])}
    def g(k):
        d=pi.get(k) or {}; v=d.get('price_index_value') or 0
        return (v or None, d.get('min_price') or None)
    e,o,s=g('external_index_data'),g('ozon_index_data'),g('self_marketplaces_index_data')
    rows.append(dict(offer=x['offer_id'],sku=i.get('sku') or '',pid=x['product_id'],name=i.get('name',''),
      fbo=st.get('fbo',0),fbs=st.get('fbs',0),price=float(pr['price']),mkt=float(pr['marketing_seller_price'] or pr['price']),
      old=float(pr['old_price'] or 0),minp=float(pr['min_price'] or 0),color=pi['color_index'],e=e,o=o,s=s,
      cost=cost.get(x['offer_id'],0),comm=cm.get('sales_percent_fbo',0),
      dl=cm.get('fbo_deliv_to_customer_amount',0) or 0,lmin=cm.get('fbo_direct_flow_trans_min_amount',0) or 0,lmax=cm.get('fbo_direct_flow_trans_max_amount',0) or 0,acq=x.get('acquiring',0) or 0))
rows.sort(key=lambda r:(ORD[r['color']],-(r['fbo']+r['fbs'])))
def profit(P,r):
    b=P-P*r['comm']/100-(r['dl']+r['lmin']+LK*(r['lmax']-r['lmin']))-r['acq']-r['cost']; return b*(1-TAX) if b>0 else b
wb=openpyxl.Workbook()
H=Font(bold=True,color='FFFFFF'); HF=PatternFill('solid',fgColor='1F4E78')
def head(ws,cols,w):
    ws.append(cols)
    for j,c in enumerate(cols,1):
        cell=ws.cell(1,j); cell.font=H; cell.fill=HF; cell.alignment=Alignment(wrap_text=True,vertical='center')
        ws.column_dimensions[L(j)].width=w[j-1]
    ws.row_dimensions[1].height=45; ws.freeze_panes='E2'; ws.auto_filter.ref=f"A1:{L(len(cols))}1"
# Настройки
st=wb.active; st.title='Индекс цен'
ns=wb.create_sheet('Настройки')
ns.append(['Параметр','Значение','Пояснение'])
ns.append(['УСН «доходы минус расходы»',TAX,'Налог с прибыли до налога'])
ns.append(['Целевой индекс',TGT,'До какого индекса снижать цену (по худшему из индексов). 1,00 — не дороже минимальной цены у конкурентов'])
ns.append(['Минимальная маржа',MM,'Ниже этой маржи (прибыль / цена) снижать цену не рекомендуем. Поставьте своё значение'])
for c in 'ABC': ns[c+'1'].font=Font(bold=True)
ns.append(['Логистика FBO: точка между мин. и макс. тарифом',LK,'0 — мин. тариф (всё локально), 1 — макс. тариф, 0,5 — середина. Подберите по своей доле локальных продаж'])
ns['B5'].number_format='0%'
ns['B2'].number_format=ns['B4'].number_format='0%'; ns.column_dimensions['A'].width=30; ns.column_dimensions['C'].width=90
cols=['Артикул','SKU','Product ID','Название','Остаток FBO','Остаток FBS','Цена продавца, ₽','Цена с акциями продавца, ₽','Цена до скидки, ₽','Мин. цена, ₽',
 'Индекс цен (цвет)','Индекс: внешние площадки','Мин. цена: внешние, ₽','Индекс: Ozon','Мин. цена: Ozon, ₽','Индекс: ваши площадки','Мин. цена: ваши, ₽','Худший индекс',
 'Себестоимость, ₽ (TrueStats)','Комиссия FBO, %','Логистика FBO, ₽','Эквайринг, ₽','Прибыль сейчас, ₽','Маржа сейчас','Рекоменд. цена, ₽ (оценка)','Прибыль при рекоменд., ₽','Маржа при рекоменд.','Рекомендация','Доставка до покупателя, ₽','Логистика мин., ₽','Логистика макс., ₽']
head(st,cols,[14,12,12,45,9,9,11,11,11,10,14,11,11,11,11,11,11,10,12,10,10,10,11,9,12,12,10,45,11,11,11])
for n,r in enumerate(rows,2):
    v=lambda t:t if t is not None else None
    st.append([r['offer'],r['sku'],r['pid'],r['name'],r['fbo'],r['fbs'],r['price'],r['mkt'],r['old'],r['minp'],CN[r['color']],
      r['e'][0],r['e'][1],r['o'][0],r['o'][1],r['s'][0],r['s'][1],
      f'=IF(COUNT(L{n},N{n},P{n})=0,"",MAX(L{n},N{n},P{n}))',
      r['cost'] or None,r['comm'],f'=AC{n}+AD{n}+Настройки!$B$5*(AE{n}-AD{n})',r['acq'],
      f'=IF(S{n}="","",IF(H{n}-H{n}*T{n}/100-U{n}-V{n}-S{n}>0,(H{n}-H{n}*T{n}/100-U{n}-V{n}-S{n})*(1-Настройки!$B$2),H{n}-H{n}*T{n}/100-U{n}-V{n}-S{n}))',
      f'=IF(W{n}="","",W{n}/H{n})',
      f'=IF(AND(OR(K{n}="Невыгодная",K{n}="Умеренная"),R{n}<>""),IF(R{n}>Настройки!$B$3,ROUNDDOWN(H{n}*Настройки!$B$3/R{n},0),""),"")',
      f'=IF(OR(Y{n}="",S{n}=""),"",IF(Y{n}-Y{n}*T{n}/100-U{n}-V{n}-S{n}>0,(Y{n}-Y{n}*T{n}/100-U{n}-V{n}-S{n})*(1-Настройки!$B$2),Y{n}-Y{n}*T{n}/100-U{n}-V{n}-S{n}))',
      f'=IF(Z{n}="","",Z{n}/Y{n})',
      f'=IF(E{n}+F{n}=0,"Нет остатка — не в приоритете",IF(OR(K{n}="Супервыгодная",K{n}="Выгодная"),"Держать цену",IF(K{n}="Нет индекса","Нет индекса — сравнить вручную",IF(S{n}="","Нет себестоимости — внести в TrueStats",IF(Y{n}="","Индекс ≤ целевого, но цвет не зелёный — проверить в ЛК",IF(AA{n}>=Настройки!$B$4,"Снизить до "&Y{n}&" ₽",IF(Z{n}>0,"Снижение даст маржу ниже порога — решить вручную","Не снижать: уйдём в минус. Работать с закупкой/карточкой")))))))',r['dl'],r['lmin'],r['lmax']])
    for c in 'XAA': pass
N=len(rows)+1
for c in ('X','AA'):
    for k in range(2,N+1): st[f'{c}{k}'].number_format='0%'
for c in ('W','Z'):
    for k in range(2,N+1): st[f'{c}{k}'].number_format='0'
fills={'Невыгодная':'F8CBAD','Умеренная':'FFE699','Выгодная':'C6EFCE','Супервыгодная':'8FD19E'}
for t,col in fills.items():
    st.conditional_formatting.add(f'K2:K{N}',FormulaRule(formula=[f'$K2="{t}"'],fill=PatternFill('solid',fgColor=col)))
st.conditional_formatting.add(f'W2:W{N}',FormulaRule(formula=['AND(W2<>"",W2<0)'],font=Font(color='C00000',bold=True)))
# Новые цены (снимок)
nc=wb.create_sheet('Новые цены',1)
head(nc,['Артикул','Product ID','Название','Индекс (цвет)','Худший индекс','Цена сейчас, ₽','Новая цена, ₽','Изменение, %','Цена до скидки, ₽','Мин. цена сейчас, ₽','Мин. цена новая, ₽','Прибыль сейчас, ₽','Прибыль при новой, ₽','Маржа при новой','Остаток FBO+FBS','Утверждаю (да/нет)','Комментарий'],
     [14,12,45,13,9,10,10,9,10,10,10,10,10,9,9,12,30])
nc.freeze_panes='D2'; cnt=0
for r in rows:
    if r['color'] not in ('RED','YELLOW') or not r['cost'] or r['fbo']+r['fbs']==0: continue
    idx=[t for t in (r['e'][0],r['o'][0],r['s'][0]) if t]
    if not idx or max(idx)<=TGT: continue
    P=int(r['mkt']*TGT/max(idx)); pn=profit(P,r)
    if pn/P<MM: continue
    cnt+=1
    nc.append([r['offer'],r['pid'],r['name'],CN[r['color']],max(idx),r['mkt'],P,(P-r['mkt'])/r['mkt'],r['old'],r['minp'],min(r['minp'],P) if r['minp'] else None,round(profit(r['mkt'],r)),round(pn),pn/P,r['fbo']+r['fbs'],'',''])
    k=nc.max_row; nc[f'H{k}'].number_format=nc[f'N{k}'].number_format='0%'
# Сводка
sv=wb.create_sheet('Сводка',0)
today=datetime.date.today().isoformat()
sv.append(['Индекс цен Ozon — AlisaFox']); sv['A1'].font=Font(bold=True,size=14)
sv.append([f'Данные: Ozon Seller API (/v5/product/info/prices, /v3/product/info/list) на {today}; себестоимость — TrueStats. Товаров: {len(rows)}.'])
sv.append([]); sv.append(['Цвет индекса','Товаров','С остатком']); 
for c in 'ABC': sv[c+'4'].font=Font(bold=True)
for k in ['Невыгодная','Умеренная','Выгодная','Супервыгодная','Нет индекса']:
    r=sv.max_row+1
    sv.append([k,f"=COUNTIF('Индекс цен'!K:K,A{r})",f"=COUNTIFS('Индекс цен'!K:K,A{r},'Индекс цен'!E:E,\">0\")+COUNTIFS('Индекс цен'!K:K,A{r},'Индекс цен'!E:E,0,'Индекс цен'!F:F,\">0\")"])
sv.append([]); sv.append(['Без себестоимости в TrueStats',f"=COUNTBLANK('Индекс цен'!S2:S{N})"])
sv.append(['Снизить цену (маржа выше порога)',f"=COUNTIF('Индекс цен'!AB:AB,\"Снизить до*\")"])
sv.append(['В минусе при текущей цене',f"=COUNTIF('Индекс цен'!W2:W{N},\"<0\")"])
sv.append(['Строк на листе «Новые цены»',cnt])
sv.column_dimensions['A'].width=40; sv.column_dimensions['B'].width=12; sv.column_dimensions['C'].width=12
# Как работать
hw=wb.create_sheet('Как работать')
for line in [
 'Как устроена таблица',
 '1. «Индекс цен» — все товары Ozon. Сверху невыгодные и умеренные, внутри — по остатку.',
 '2. Индексы и минимальные цены — из Ozon API. Ozon сравнивает цену для покупателя (с его скидками), а не вашу цену продавца, поэтому рекомендованная цена — оценка: цена × целевой индекс / худший индекс.',
 '3. Прибыль = (цена с акциями − комиссия FBO − логистика FBO (между мин. и макс. тарифом Ozon, см. «Настройки») − эквайринг − себестоимость) × (1 − УСН 7%). Без хранения, возвратов, рекламы и платной приёмки — реальная прибыль ниже.',
 '4. Себестоимость правьте в столбце S или в TrueStats — прибыль и рекомендации пересчитаются.',
 '5. «Новые цены» — снимок на дату выгрузки: невыгодные и умеренные товары с остатком, где снижение сохраняет маржу не ниже порога. Ставьте «да» в «Утверждаю».',
 '6. Цены в кабинете меняет команда: вручную или загрузкой. Ozon не даст поставить новую цену ниже мин. цены — её тоже меняйте (столбец «Мин. цена новая»).',
 '7. После смены цены индекс в Ozon обновляется не сразу. Проверяйте цвет на следующий день.',
 '',
 'Команды для обновления (пишите в проекте «Промт инженер»)',
 '«Обнови индекс цен» — свежая выгрузка, тот же формат.',
 '«Что изменилось в индексе с прошлого раза» — какие товары сменили цвет.',
 '«Проверь новые цены» — после смены цен сверить индекс и прибыль.',
 '«Поменяй порог маржи на 15%» — пересчитать «Новые цены».']:
    hw.append([line])
hw['A1'].font=hw['A10'].font=Font(bold=True); hw.column_dimensions['A'].width=140
wb.move_sheet('Настройки',offset=10)
wb.save(OUT); print(len(rows),cnt,sum(1 for r in rows if not r['cost']))
