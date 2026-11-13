package io.astrbot.android

import io.astrbot.android.core.ServiceId

/** 完整版只做 AstrBot webchat；NapCat/QQ 保留代码但不进 UI、不参与“全部启动”。 */
internal val visibleServiceIds: List<ServiceId>
    get() = if (BuildConfig.WEBCHAT_ONLY) listOf(ServiceId.ASTRBOT) else ServiceId.entries
