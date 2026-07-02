"""배포된 인스턴스에 데모/실측을 HTTP 로 흘려넣는 스크립트.

사용:
    python scripts/seed_demo.py --url https://<앱>.up.railway.app        # 과거 이력 backfill
    python scripts/seed_demo.py --url http://localhost:8000 --live       # 실시간 스트리밍
"""
import argparse, json, math, random, time, urllib.request

DEVICES = [
    ("FGP-L2","경화제 FGP 서보","도장","A",8.0,9.6,12.0),
    ("CONV-L2","L2 컨베어 구동","도장","A",12.0,15.0,19.0),
    ("COMP-01","컴프레서 #1","유틸","A",30.0,38.0,48.0),
    ("INJ-HYD-1","1호기 유압펌프","사출","A",45.0,55.0,68.0),
    ("HEATER-3","3호기 노즐히터","사출","A",6.0,9.0,11.0),
]


def post(url, path, body):
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def value(dev, prog, i, rnd, nominal):
    if dev == "FGP-L2":
        return round(max(0, nominal*(1+0.24*prog) + math.sin(i/9)*0.35 + rnd.gauss(0,0.25)), 3)
    if dev == "HEATER-3":
        return 0.0 if prog > 0.9 else round(max(0, nominal + rnd.gauss(0,0.15)), 3)
    a = nominal*0.06
    return round(max(0, nominal + math.sin(i/11)*a + rnd.gauss(0,a*0.6)), 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--live", action="store_true", help="실시간 스트리밍(무한)")
    ap.add_argument("--n", type=int, default=200)
    a = ap.parse_args()

    for dev, label, grp, unit, nom, soft, hard in DEVICES:
        post(a.url, "/api/config", dict(device=dev, label=label, grp=grp, unit=unit,
                                        nominal=nom, soft=soft, hard=hard))

    if a.live:
        print("실시간 스트리밍… Ctrl+C 로 중단")
        i = 0
        rnds = {d[0]: random.Random(i) for d in DEVICES}
        while True:
            for dev, *_rest, nom, soft, hard in DEVICES:
                v = value(dev, min(1.0, i/500), i, rnds[dev], nom)
                post(a.url, "/ingest", dict(device=dev, irms=v))
            i += 1; time.sleep(2)
    else:
        now = time.time(); step = 30
        for dev, label, grp, unit, nom, soft, hard in DEVICES:
            rnd = random.Random(hash(dev) % 9999)
            for i in range(a.n):
                ts = now - (a.n - i) * step
                v = value(dev, i/(a.n-1), i, rnd, nom)
                post(a.url, "/ingest", dict(device=dev, irms=v, ts=ts))
            print(f"{dev}: {a.n} points")
        print("backfill 완료")


if __name__ == "__main__":
    main()
