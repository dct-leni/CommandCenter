// --- Media autoplay & audio sink selection always allowed ---
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
user_pref("media.autoplay.allow-muted", true);
user_pref("media.autoplay.enabled.user-gestures-needed", false);
user_pref("permissions.default.autoplay-media", 1);
// --- Disable First Run / Welcome / Onboarding Pages ---
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.onboarding.enabled", false);
user_pref("browser.onboarding.hidden", true);
user_pref("browser.onboarding.notification.finished", true);
user_pref("browser.uitour.enabled", false);
user_pref("datareporting.policy.dataSubmissionPolicyAcceptedVersion", 999);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.rights.3.shown", true);
user_pref("browser.rights.override", "show");
user_pref("browser.rights.silence", true);
user_pref("browser.tos.accepted", true);
user_pref("browser.tos.shown", true);
user_pref("browser.newtabpage.introShown", true);
user_pref("trailhead.firstrun.didSeeAboutWelcome", true);
// --- Enable unsigned extensions & stylesheet customization ---
user_pref("xpinstall.signatures.required", false);
user_pref("extensions.experiments.enabled", true);
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);
// --- Optimize for background rendering and GDI capture ---
user_pref("dom.suspend_inactive.enabled", false);
user_pref("dom.timeout.enable_budget_timer_throttling", false);
user_pref("widget.windows.window_occlusion_tracking.enabled", false);
user_pref("dom.ipc.processPriorityManager.backgroundUsesEcoQoS", false);
user_pref("network.http.throttle.enable", false);
user_pref("media.block-autoplay-until-in-foreground", false);
// --- Memory-Only Caching (Zero Disk Accumulation on 24/7 streams) ---
user_pref("browser.cache.disk.enable", false);
user_pref("browser.cache.memory.enable", true);
user_pref("browser.cache.memory.capacity", 65536);
user_pref("browser.sessionhistory.max_entries", 2);
// --- Force Software WebRender (Direct GDI window compatibility) ---
user_pref("gfx.webrender.all", false);
user_pref("gfx.webrender.software", true);
user_pref("layers.acceleration.disabled", true);
// --- Cookie / Banner handling ---
user_pref("cookiebanners.service.mode", 2);
user_pref("cookiebanners.service.mode.privateBrowsing", 2);
user_pref("privacy.donottrackheader.enabled", true);
// --- Disable popups & notifications ---
user_pref("dom.disable_open_during_load", true);
user_pref("permissions.default.desktop-notification", 2);
user_pref("dom.webnotifications.enabled", false);
// --- Enable Widevine CDM & DRM Playback ---
user_pref("browser.crashReports.unsubmittedCheck.enabled", false);
user_pref("browser.crashReports.unsubmittedCheck.autoSubmit2", false);
user_pref("media.eme.enabled", true);
user_pref("media.gmp-widevinecdm.enabled", true);
user_pref("media.gmp-widevinecdm.visible", true);
user_pref("media.gmp-widevinecdm.autoupdate", true);
user_pref("media.gmp-provider.enabled", true);
user_pref("media.gmp-manager.updateEnabled", true);
user_pref("media.gmp.decoder.enabled", true);
// --- WebRTC / Network Prefs ---
user_pref("media.peerconnection.enabled", false);
user_pref("intl.accept_languages", "tr-TR, tr, en-US, en");
user_pref("javascript.use_us_english_locale", false);
// --- Force Reliable TCP over SOCKS5 / Proxy ---
user_pref("network.http.http3.enable", false);
user_pref("network.http.spdy.enabled.http2", true);
user_pref("network.dns.disableIPv6", true);
// --- Minimize Child Process Count ---
user_pref("dom.ipc.processCount", 1);
user_pref("dom.ipc.processCount.webIsolated", 1);