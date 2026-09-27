# 搬瓦工流量显示

此 fork 的中文安装脚本支持在现有 Clash Verge Rev 订阅卡片中显示搬瓦工 VPS 的已用流量和月度额度。默认关闭，启用后沿用原订阅地址、TLS 证书和节点配置。

## 启用

使用 root 用户运行脚本。要求服务器使用 systemd，并安装 Python 3.8 或更新版本。缺少 Python 时菜单会通过系统包管理器安装；Alpine/OpenRC 暂不支持此可选功能。

1. 使用本 fork 的中文安装脚本；已有安装可以先下载本 fork 的新版脚本运行，无需重装代理核心。
2. 在 KiwiVM 控制面板的 API 页面获取这台 VPS 的 VEID 和 API Key。
3. 执行 `vasma`，进入 **7.账号管理 → 2.查看订阅**，确保订阅服务已经建立。已有订阅可跳过此步。
4. 进入 **7.账号管理 → 6.搬瓦工流量显示 → 1.启用/修改凭据**。
5. 输入 VEID 和 API Key。密钥输入不回显。程序会先验证 API 返回值，再更新已有订阅的 Nginx 配置。
6. 在 Clash Verge Rev 中更新原来的完整配置订阅（地址包含 `/s/clashMetaProfiles/`），并设置订阅自动更新，例如每 10 分钟一次。

API Key 是 VPS 管理凭据。只在自己的服务器输入，不要放入订阅 URL、YAML、截图或提交到 GitHub。它保存在 `/etc/v2ray-agent/bwg-traffic/config.json`，目录权限为 700、文件权限为 600。不要与他人分享这个文件。

## 统计口径与刷新

- 这是对应 VPS 的整体用量，包含其他设备、其他代理用户和服务器程序产生的计费流量。所有该服务器的订阅用户看到同一个值。
- 合并其他 VPS 节点后，这个值仍然仅代表配置的搬瓦工 VPS。
- 使用 API 返回的实际额度，不将 500G 写死。按 KiwiVM 文档对 `data_counter` 和 `plan_monthly_data` 同时应用 `monthly_data_multiplier`。
- `download` 字段承载汇总用量，`upload` 为 0；不代表真实的单向下载统计。
- 不填写 `expire`：流量重置日不等于订阅到期日。
- systemd timer 约每 5 分钟查询一次 API。Clash Verge 的订阅刷新是另一个周期，另外还有服务商自身的统计延迟，因此不是秒级实时。
- API 失败时短暂保留最后成功值；在后续失败检查发现它已超过 15 分钟时移除流量头。订阅正文继续由 Nginx 提供，后续 API 恢复后自动重新显示。
- 查询成功但数值没变化时只更新本地新鲜度，不 reload。数值变化时执行 `nginx -t` 后平滑 reload，不调用脚本原有的 stop/start 操作。大量长期连接会延迟旧 Nginx worker 退出；这类部署可适当延长 timer 间隔。
- 启用前已经在客户端缓存的旧流量信息是否立即消失，取决于客户端版本及是否重新更新了订阅。不要把旧卡片当作实时读数。

## 查看、刷新与关闭

菜单中的 **2.立即刷新** 可手动触发服务端查询；之后在 Clash Verge 中更新订阅。

~~~bash
systemctl status v2ray-agent-bwg.timer
systemctl list-timers v2ray-agent-bwg.timer
journalctl -u v2ray-agent-bwg.service -n 30 --no-pager
nginx -t
~~~

在自己的终端检查订阅响应头（替换示例地址，不要公开完整订阅链接）：

~~~bash
curl --silent --show-error --dump-header - --output /dev/null \
  'https://你的域名:订阅端口/s/clashMetaProfiles/你的订阅ID'
~~~

应出现 `Subscription-Userinfo: upload=0; download=...; total=...`，其中数值为字节。首次启用时与 KiwiVM 的 Bandwidth usage 核对。Nginx 转发层或 CDN 应保留这个响应头，并尊重 `Cache-Control: private, no-store`。

选择 **3.关闭并删除保存的凭据** 会删除用量响应头和凭据，撤销 timer。原订阅继续使用；通配符 include 保留为空，便于以后再次启用。完整卸载脚本时也会清理 timer。

配置或 reload 失败会恢复本次改动前的配置文件。先检查 `nginx -t`、API 凭据及服务器到 `api.64clouds.com` 的连通性，再重试启用。错误日志只记录异常类型，不记录密钥或 API 返回的账户信息。

## 更新与重新生成订阅

此 fork 的中文安装入口和“更新脚本”入口均指向 `Kenny-Xiang/v2ray-agent`，避免更新时覆盖可选功能。不要混用上游安装命令或英文安装脚本；英文脚本尚未集成此功能。

新增订阅的 Nginx 模板包含流量配置引用，已有安装通过启用菜单补上引用。重新生成节点文件不会改变凭据。如果外部脚本覆盖了 Nginx 的订阅配置，请重新启用此功能恢复引用。

## 验证开发改动

~~~bash
bash -n install.sh
python3 -m unittest discover -s tests -p 'test_bandwagon_traffic.py'
BWG_TEST_NGINX=/usr/sbin/nginx python3 -m unittest discover -s tests -p 'test_bandwagon_traffic.py'
~~~

最后一条额外启动临时 Nginx，在本机随机端口验证完整配置和节点列表的响应头、原订阅正文、禁用后的行为。测试使用模拟 API 数据，不访问真实 VPS 或真实 KiwiVM 凭据。

参考：[Clash Verge Rev 订阅响应头](https://www.clashverge.dev/guide/url_schemes.html)、[KiwiVM API 文档公开副本](https://github.com/dhslegen/bandwagon-dashboard/blob/main/Bandwagon%20Host%20REST%20API.md)、[Nginx 响应头](https://nginx.org/en/docs/http/ngx_http_headers_module.html)、[Nginx 平滑重载](https://nginx.org/en/docs/control.html)。KiwiVM 最新接口文档请以账户控制面板内的 API 页面为准。
