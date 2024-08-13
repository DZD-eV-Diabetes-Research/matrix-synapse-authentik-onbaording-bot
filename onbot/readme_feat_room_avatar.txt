curl 'https://chat.mycom.org/_matrix/media/v3/upload?filename=wtf.webp' \
  -X 'POST' \
  -H 'accept: */*' \
  -H 'accept-language: en-US,en;q=0.9,de;q=0.8' \
  -H 'authorization: Bearer xxx' \
  -H 'cache-control: no-cache' \
  -H 'content-length: 2204164' \
  -H 'content-type: image/webp' \
  -H 'origin: https://chat.mycom.org' \
  -H 'pragma: no-cache' \
  -H 'priority: u=1, i' \
  -H 'sec-ch-ua: "Chromium";v="127", "Not)A;Brand";v="99"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Linux"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
  
  
curl $'https://chat.mycom.org/_matrix/client/v3/rooms/\u0021dEOGmBUmjouGgJYbcg%3Amycom.org/state/m.room.avatar/' \
  -X 'PUT' \
  -H 'accept: application/json' \
  -H 'accept-language: en-US,en;q=0.9,de;q=0.8' \
  -H 'authorization: Bearer xxx' \
  -H 'cache-control: no-cache' \
  -H 'content-type: application/json' \
  -H 'origin: https://chat.mycom.org' \
  -H 'pragma: no-cache' \
  -H 'priority: u=1, i' \
  -H 'sec-ch-ua: "Chromium";v="127", "Not)A;Brand";v="99"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Linux"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36' \
  --data-raw '{"url":"mxc://mycom.org/QPiItwwHGtfGJUfJLeSXVpxD"}'
  

m.room.avatar


curl 'https://chat.mycom.org/_matrix/client/v3/profile/%40tim.bleimehl%3Amycom.org' \
  -H 'accept: application/json' \
  -H 'accept-language: en-US,en;q=0.9,de;q=0.8' \
  -H 'authorization: Bearer syt_dGltLmJsZWltZWhs_HOgCZACVBcUdvwFxbgwb_35YyVQ' \
  -H 'cache-control: no-cache' \
  -H 'pragma: no-cache' \
  -H 'priority: u=1, i' \
  -H 'sec-ch-ua: "Chromium";v="127", "Not)A;Brand";v="99"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Linux"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'