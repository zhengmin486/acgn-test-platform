"""应用配置：从环境变量 / .env 读取，密码等敏感信息不写入代码。

DATABASE_URL 取值示例：
- MySQL（目标生产配置）：
  mysql+pymysql://acgn_app:密码@127.0.0.1:3306/acgn_test_platform?charset=utf8mb4
- SQLite（仅本地开发与自动化验证，不是最终生产方案）：
  sqlite:///./local_dev.db
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./local_dev.db"

    # ---------- 认证与访问控制 ----------
    # 会话 Cookie 的签名密钥；必须通过 .env 覆盖后再部署（默认值仅供本地开发）。
    auth_secret_key: str = "dev-only-insecure-secret-change-in-env"
    # 会话有效期（秒），默认 8 小时。
    session_max_age_seconds: int = 8 * 60 * 60
    # users 表为空时创建的初始账号（仅首次初始化生效，不覆盖已有用户）。
    # 生产环境必须通过 .env 覆盖默认口令。
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123456"
    default_user_username: str = "demo"
    default_user_password: str = "demo123456"

    # ---------- 邮箱验证（可插拔） ----------
    # 总开关：置为 False 后注册即登录、登录不再校验邮箱，行为完全退回加该功能之前。
    email_verification_enabled: bool = True
    # 验证令牌有效期（秒），默认 24 小时。
    email_verification_token_ttl_seconds: int = 24 * 60 * 60
    # 找回密码令牌有效期（秒），默认 1 小时——重置链接权限更高，有效期刻意
    # 比邮箱验证短很多。
    password_reset_token_ttl_seconds: int = 60 * 60
    # 同一账号两次发送验证邮件之间的冷却时间（秒），防止被拿来轰炸邮箱。
    email_resend_cooldown_seconds: int = 60
    # 站点对外基址（如 https://acgn.example.com）；留空时取当前请求的 host。
    public_base_url: str = ""

    # ---------- 注册控制 ----------
    # 注册模式：
    # - invite（默认）：关闭公开注册，/register 必须提供有效邀请码
    #   （管理员在 /admin/invites 生成，或 scripts/create_invite.py 批量生成）；
    # - open：任何人可自助注册（加邀请码功能之前的旧行为）。
    # 任何非法取值都按 invite 处理（安全默认），只有显式 open 才放开发布。
    registration_mode: str = "invite"

    # ---------- 御宅成分检测（Bangumi 公开收藏 + 可选 LLM） ----------
    # Bangumi API 基址，一般不用改。
    bangumi_api_base: str = "https://api.bgm.tv"
    # 详情页元数据补全单独走哪个基址：留空则沿用 bangumi_api_base。
    # 单独一项是为了**只**让作品资料补全指向可用镜像，不牵动上面成分检测的
    # 收藏同步（线上服务器直连 api.bgm.tv 被 DNS 污染，镜像 bgmapi.anibt.net 可用）。
    bangumi_meta_api_base: str = ""
    # Bangumi 要求可识别的 User-Agent；这里带上项目标识。
    bangumi_user_agent: str = "acgn-persona/0.1 (local dev)"
    # 出网代理（如 http://127.0.0.1:9910）；留空则用系统默认（含环境变量代理）。
    bangumi_proxy: str = ""
    # 单次同步最多拉取多少条收藏，避免一次请求过久。
    bangumi_max_items: int = 1000
    # 单次「同步到本站」最多新建多少部作品（收藏上千条时防止一次点击写爆作品库）；
    # 达到上限后页面会提示「可再点一次继续」。管理员 / 站长用下面更大的上限。
    persona_site_sync_limit: int = 200
    persona_site_sync_limit_staff: int = 1000
    # 免登录试算（引流用）：最多拉多少条收藏、同一 IP 多少秒内只允许一次。
    # 试算结果不落库，只渲染当次页面；登录后才保存快照与报告。
    persona_guest_max_items: int = 200
    # B站游客试算单独压到 100：翻页少、响应快，也降低被当免费代理刷的风险。
    persona_guest_bilibili_max_items: int = 100
    # 20 秒防抖：防双击 / 连点，不占额度（与下面的日限正交）。
    persona_guest_cooldown_seconds: int = 20
    # 游客试算双闸日限（24 小时滑动窗口）：
    # - 设备闸：签名的设备 Cookie（HttpOnly），同一设备每天最多 3 次；
    #   清 Cookie / 无痕可绕过，所以再叠一层 IP 闸；
    # - IP 闸：同一 IP 每天最多 10 次（放宽：NAT / 校园网后多设备共用出口，
    #   额度给足才不误伤，只用来兜住「清 Cookie 连刷」的脚本）；
    # - 登录用户不受限（游走路：登录后绑定数据源随便同步）。
    persona_guest_device_daily_limit: int = 3
    persona_guest_ip_daily_limit: int = 10
    # 详情页按需补全制作 / 声优 / 客观信息；**不在请求内同步执行**，交给后台任务，
    # 失败写 meta_failed_at 进入冷却（见 app/business_pages._refresh_content_assets）。
    # 批量补全用 scripts/backfill_meta.py，产物一致。
    bangumi_meta_on_demand: bool = True
    # 补全结果的缓存有效期（天）：同一部作品超过这个时间再次访问才重拉。
    bangumi_meta_ttl_days: int = 30
    # 后台补全的单接口超时（秒）。比 bangumi_timeout 短：后台任务不该长占线程，
    # 拉不到就等冷却结束后再试，而不是把三接口的超时叠起来。
    bangumi_meta_timeout: float = 5.0
    # 补全失败后的冷却（小时）：冷却期内详情页不再发起请求。
    # 线上 Bangumi 被污染时，没有冷却的旧实现会让每个详情页每次都等满超时。
    bangumi_meta_retry_cooldown_hours: int = 6
    # Bangumi 请求超时（秒）。
    bangumi_timeout: float = 15.0
    # B站追番表（成分检测第二数据源）：匿名公开接口即可读，无需登录 / cookie。
    # 国内站点直连；UA 必须是浏览器形态，否则容易触发 -352 风控。
    bilibili_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # 单次同步最多拉取多少条追番（ps 上限 30，翻页拉取）。
    bilibili_max_items: int = 500
    # B站接口超时（秒）。
    bilibili_timeout: float = 15.0
    # 可选：LLM 文案生成。国内主流三家都兼容 OpenAI 的 /chat/completions 协议，
    # 只填 PERSONA_LLM_PROVIDER + PERSONA_LLM_API_KEY 即可，地址与模型按预设自动补：
    #   deepseek → https://api.deepseek.com                 / deepseek-chat
    #   doubao   → https://ark.cn-beijing.volces.com/api/v3 / 需填推理接入点 ID(ep-…) 或模型名
    #   glm      → https://open.bigmodel.cn/api/paas/v4     / glm-4-flash
    # 显式填的 BASE_URL / MODEL 优先，因此也能指向任意兼容服务；三项配齐才启用「直接生成」，
    # 否则页面只提供「复制提示词 → 千问 / 豆包 / DeepSeek 网页版 → 贴回」的手动模式。
    persona_llm_provider: str = ""
    persona_llm_base_url: str = ""
    persona_llm_api_key: str = ""
    persona_llm_model: str = ""
    # 单次生成的输出上限；报告较长，给足 token 避免被截断（0 表示不传，用服务端默认）。
    persona_llm_max_tokens: int = 2000

    # ---------- 开发种子数据 ----------
    # contents 为空时是否写入开发假数据（app/fake_data.py）。
    # 开发/测试保持 True；上线置 False 用干净库，再经后台「数据同步」灌真实数据。
    seed_demo_data: bool = True

    # ---------- 数据库备份（scripts/backup_db.py） ----------
    # 备份输出目录（相对项目根或绝对路径）与保留份数（0 表示不清理）。
    backup_dir: str = "backups"
    backup_retain: int = 14

    # ---------- 增量入库通道（app/ingest_api.py） ----------
    # 服务器无境外出网，作品数据由本机抓取后 POST 回服务器入库；请求头 X-Ingest-Key
    # 比对这个值。留空 = 端点一律 503（默认关闭），配了才启用。
    # 只放生产 .env（chmod 600），不要提交进仓库。
    ingest_api_key: str = ""

    # ---------- 封面图 ----------
    # 封面本地存储目录：Bangumi 导入时下载到此，Content.image_url 存站内
    # 相对地址（/covers/...）。默认在项目内；想放到其它盘符（如 D:/acgn-covers）
    # 改成绝对路径即可，main.py 会原样挂载到 /covers。
    cover_storage_dir: str = "app/static/covers"
    # 导入时是否下载封面；置 False 则只记录远程 URL（不推荐，热链易失效）。
    cover_download_enabled: bool = True

    # ---------- 用户头像 ----------
    # 头像文件目录（挂到 /avatars）；默认在项目内，也可指到其它盘符。
    avatar_storage_dir: str = "app/static/avatars"
    # 上传原图大小上限：头像会被裁成 256×256 WebP，2MB 足够。
    avatar_upload_max_bytes: int = 2 * 1024 * 1024

    # ---------- 资讯图片 ----------
    # 资讯正文图与头图的本地目录（挂到 /article-images）；相对路径按项目根解析。
    article_image_storage_dir: str = "app/static/article_images"
    # 单张资讯图大小上限（字节）：只认魔术字节，不信 URL 后缀与 Content-Type。
    article_image_max_bytes: int = 5 * 1024 * 1024
    # 导入/回填时是否下载资讯图；关闭时只记源站外链（测试里全局关闭，避免触网）。
    article_image_download_enabled: bool = True

    # ---------- 多源资讯每日同步（scripts/news_daily.py） ----------
    # 每天新入库资讯总上限（全站、不分源）。cron 每 4 小时跑一次也按自然日累计，
    # 当天已入库多少就从配额里扣多少，跑满即停。
    # 取数口径：日语源优先于中文源；题材优先级 动画 > 漫画 > 轻小说 > 游戏 > 其他。
    news_daily_quota: int = 30
    # 「重大官宣」豁免额度（条/天）：动画化 / 制作決定 / 续作決定 这类官宣不受上面的
    # 配额限制，走这个独立额度入库（不消耗 news_daily_quota）。0 = 关闭豁免。
    # 豁免条目落 Article.is_major，额度按自然日累计（cron 每轮独立进程，靠库统计）。
    news_exempt_max_per_day: int = 10
    # 停用的资讯源（逗号分隔 key，如 "gamersky,natalie_comic"）：
    # run_import_daily 直接跳过这些源——不改代码即可下线一个源。留空 = 全部启用。
    news_disabled_sources: str = ""
    # 日文源自动翻译（OpenAI 兼容接口）：NEWS_LLM_* 留空则回退 PERSONA_LLM_*，
    # 两边都没配就跳过日文源、只用中文源（不产出看不懂的日文条目）。
    news_llm_provider: str = ""
    news_llm_base_url: str = ""
    news_llm_api_key: str = ""
    news_llm_model: str = ""
    news_translate_timeout: float = 60.0

    # ---------- 成人内容展示（不是年龄验证 / 分级） ----------
    # 全局「隐藏成人内容」开关：开启时，标记 is_adult 的资讯在展示层隐藏头图与露骨摘要，
    # 用普通占位提示替代；作品名 / 发售日 / 非露骨资讯仍正常展示。默认开启。
    # 这是展示偏好而非权限系统——不实现年龄验证、不扩展账号权限。
    news_hide_adult_content: bool = True

    # ---------- 封面图片处理（app/media.py） ----------
    # 列表缩略图宽度（px）：只缩不放，输出 WebP；列表页流量主要靠它省。
    cover_thumb_width: int = 400
    # 详情全图双约束：宽度 px 与字节数；超出边缩边降画质直到达标，
    # 保底也接受最小画质——封面可见性优先于体积达标。
    cover_full_max_width: int = 1200
    cover_full_max_bytes: int = 400 * 1024
    # 管理员上传封面的大小上限（字节）；只认魔术字节，不信客户端扩展名。
    cover_upload_max_bytes: int = 5 * 1024 * 1024
    # 图片对外基址（如 https://img.example.com）：留空表示图片与应用同源。
    # 将来切 COS / CDN 只改这一项，模板里的 /media/... 自动带上前缀。
    media_base_url: str = ""

    # ---------- 邮件发送（SMTP，全部留空则不真发） ----------
    # smtp_host 与 smtp_from_address 都为空 = 不发送真实邮件：
    # 验证链接写入日志，并可在待验证页面 / 管理员页面手动生成，保证本地无邮件服务也能走完流程。
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # 465 端口用 smtp_use_ssl=True；587 端口用 smtp_use_tls=True（STARTTLS）。
    smtp_use_ssl: bool = False
    smtp_use_tls: bool = True
    smtp_from_address: str = ""
    smtp_from_name: str = "Otaku"

    # ---------- 数据库连接池 ----------
    # 仅对非 SQLite（MySQL 等生产驱动）生效；SQLite 沿用默认池，行为不变。
    # pool_recycle：连接最大复用秒数，防 MySQL wait_timeout 掐掉长闲连接。
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800
    db_pool_pre_ping: bool = True

    # ---------- 对外 API 分页上限 ----------
    # 与 api.py 各端点的 Query(le=…) 同源；改这里即改全部端点的上限。
    api_contents_max_page_size: int = 500
    api_articles_max_page_size: int = 100
    # /api/contents/search 与 /api/schedule 的每页上限（同值同语义：详情级列表）
    api_search_max_page_size: int = 100
    api_schedule_max_page_size: int = 100
    api_tags_max_limit: int = 2000
    api_sync_runs_max_limit: int = 200

    # ---------- 安全响应头 ----------
    # nosniff / Referrer-Policy 常开；X-Frame-Options 可改 SAMEORIGIN 或置空关闭。
    security_headers_enabled: bool = True
    x_frame_options: str = "DENY"

    # ---------- 静态资源与图片缓存（秒） ----------
    # style.css 带 ?v=mtime 版本号，改样式浏览器自动拉新；vendor 与封面图
    # 同名不变（上传换图生成新文件名），长缓存是安全的。0 表示不输出该头。
    static_cache_max_age: int = 86400
    media_cache_max_age: int = 604800

    # ---------- 上传 / 图片目录容量护栏 ----------
    # 封面目录（原图 + thumbs/full 派生图）总量上限：超限先删可再生派生图
    #（按需重建），仍超才删未被 contents 引用的原图（按 mtime 从旧到新）。
    # 头像目录同理（只删未被 users 引用的文件）。启动巡检一次，之后由上传
    # 路径按 check_interval 节流触发。
    media_storage_max_bytes: int = 8 * 1024 * 1024 * 1024
    avatar_storage_max_bytes: int = 256 * 1024 * 1024
    media_storage_check_interval_seconds: int = 3600
    # 原子写残留的 .tmp-* 临时文件：超过该秒数视为崩溃遗留，巡检时清理。
    media_temp_max_age_seconds: int = 3600

    # ---------- HTTP 压缩（默认关） ----------
    # 站点流量大头是已压缩的 WebP 图片，压缩它们只费 CPU 不省字节；
    # 页面以 HTML 为主且流量上来后再开。开启后仅对超过 minimum_size 的响应生效。
    gzip_enabled: bool = False
    gzip_compress_level: int = 5
    gzip_minimum_size: int = 1024

    # ---------- 日志 ----------
    # 应用日志级别（DEBUG/INFO/WARNING/ERROR）；uvicorn 访问日志由启动参数管。
    log_level: str = "INFO"

    # ---------- 短信验证码（手机绑定 / 解绑） ----------
    # 与「邮件可插拔」同一思路：sms_api_url + sms_api_key 都留空 = 不真发短信，
    # 验证码写日志并在页面上直接展示（仅测试环境可接受），保证本地无短信网关
    # 也能把绑定 / 解绑流程走完。接入具体服务商时只需要改 app/sms.py 的
    # 请求体拼装，业务代码不动。
    sms_api_url: str = ""
    sms_api_key: str = ""
    # 短信签名，多数服务商要求与报备内容一致；未配置网关时仅用于日志。
    sms_sign_name: str = "Otaku"
    # 验证码有效期（秒），默认 5 分钟——6 位数字码不能长留。
    sms_code_ttl_seconds: int = 5 * 60
    # 同一账号两次发送验证码之间的冷却时间（秒），防止被拿来轰炸手机。
    sms_resend_cooldown_seconds: int = 60

    @property
    def registration_invite_only(self) -> bool:
        """是否处于「仅邀请码注册」模式。

        只有显式配置为 open 才放开发布；任何其它取值（含拼错、留空）
        都按 invite 处理——公开注册是敏感开关，默认必须落在收紧一侧。
        """
        return self.registration_mode.strip().lower() != "open"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
