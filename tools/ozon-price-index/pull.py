import os,json,time,requests
H={"Client-Id":os.environ["OZON_CLIENT_ID"],"Api-Key":os.environ["OZON_API_KEY"]}
def post(u,b):
    for i in range(5):
        try:
            r=requests.post("https://api-seller.ozon.ru"+u,headers=H,json=b,timeout=60,verify="/root/.ccr/ca-bundle.crt");r.raise_for_status();return r.json()
        except Exception as e: print(e);time.sleep(2**i)
items=[];cur=""
while True:
    d=post("/v5/product/info/prices",{"filter":{"visibility":"ALL"},"limit":1000,"cursor":cur})
    items+=d["items"];cur=d.get("cursor")
    if not cur or not d["items"]:break
json.dump(items,open("prices.json","w"),ensure_ascii=False)
ids=[i["product_id"] for i in items];info=[]
for k in range(0,len(ids),1000):
    info+=post("/v3/product/info/list",{"product_id":ids[k:k+1000]})["items"]
json.dump(info,open("info.json","w"),ensure_ascii=False)
print(len(items),len(info))
