# yeah.zip — kya update hua

Sirf ye 2 files hain — inhi paths par apne repo mein overwrite karo:

```
main.py                 (updated — sirf docs text ki ek line)
templates/player.html   (updated — asli fix + background color)
```

## Asli bug (CORS nahi tha)

`main.py` mein pehle se hi ek robust `_raw_query_param()` tha jo aapke
signed CDN URL (jiski apni `?Signature=...&Key-Pair-Id=...&Policy=...`
query string hoti hai) ko sahi se poora padh leta tha, chahe wo
encode kiya gaya ho ya nahi.

Lekin `templates/player.html` ka JavaScript abhi bhi plain
`URLSearchParams(window.location.search).get('url')` use kar raha tha —
jo un-encoded nested URL ke PEHLE `&` par hi truncate ho jaata hai. Matlab
jab aap seedha address bar mein `/player?url=<raw m3u8 link>` paste karte
the, browser khud hi `Key-Pair-Id` aur `Policy` jaise crucial params ko
kaat deta tha (kyunki wo `url=` ke andar ke `&` ko apna hi alag param
samajh leta tha) — backend tak poora URL pahunchta hi nahi tha, isliye CDN
403/expired jaisa kuch de deta aur video kabhi play nahi hoti.

**Fix:** `player.html` mein bhi bilkul wahi raw-query-string-parsing logic
add kar di (jo `main.py` mein pehle se thi) — ab address bar mein seedha
paste kiya hua unencoded link bhi sahi se poora padha jaata hai.

**Naya convention:** agar `?mode=download` bhi chahiye to use `url` SE
PEHLE likho — `/player?mode=download&url=<m3u8>` — kyunki `url` (jab
unencoded ho) apne marker se lekar string ke end tak sab kuchh apna maan
leta hai. `main.py` ke route-list text mein bhi yehi update kar diya.

## Background color

`templates/player.html` ke `:root` CSS variables (`--bg`, `--bg-video`)
light-grey se **light-blue** kar diye — Live Player, Download Player, aur
Watch page teeno isi ek shared theme ko use karte hain, isliye teeno
automatically update ho gaye.
