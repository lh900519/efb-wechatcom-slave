# Docker

```shell
docker run -it -p 5678:5678 --name='wechatpc-com' -v "$PWD/config/:/root/.ehforwarderbot/profiles/default/" h900519/wechat-com:latest /bin/bash
```

# Config file

```YAML
server_addr: 0.0.0.0
server_port: 5678
app_id: 1234567890ABCDEFGHIJKLMNOPQRSTUV
app_key: 1234567890ABCDEFGHIJKLMNOPQRSTUV
expire: 600000
```
