include $(TOPDIR)/rules.mk

PKG_NAME:=media-downloader
PKG_VERSION:=2.0
PKG_RELEASE:=1
PKG_MAINTAINER:=OpenWrt Developer

include $(INCLUDE_DIR)/package.mk

define Package/media-downloader
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Automated Media Downloader Web Control Panel
  DEPENDS:=+python3-light +python3-urllib +python3-codecs +python3-openssl
  PKGARCH:=all
endef

define Package/media-downloader/description
  A lightweight background media sync downloader tool and web frontend interface 
  specifically engineered for capturing automated daily drama serial updates.
endef

define Build/Configure
	# Nothing to compile (Pure Script Stack)
endef

define Build/Compile
	# Nothing to compile (Pure Script Stack)
endef

define Package/media-downloader/install
	# Install background script engine
	$(INSTALL_DIR) $(1)/usr/libexec
	$(INSTALL_BIN) ./files/media-downloader.py $(1)/usr/libexec/media-downloader.py

	# Install web layout layer template
	$(INSTALL_DIR) $(1)/usr/share/media-downloader
	$(INSTALL_DATA) ./files/index.html $(1)/usr/share/media-downloader/index.html
	$(INSTALL_DATA) ./files/wallpaper.webp $(1)/usr/share/media-downloader/wallpaper.webp
	$(INSTALL_DATA) ./files/manifest.json $(1)/usr/share/media-downloader/manifest.json
	$(INSTALL_DATA) ./files/sw.js $(1)/usr/share/media-downloader/sw.js
	$(INSTALL_DATA) ./files/app-icon.png $(1)/usr/share/media-downloader/app-icon.png
	$(INSTALL_DATA) ./files/style.css $(1)/usr/share/media-downloader/style.css

	# Install default empty UCI config profile
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./files/media-downloader.config $(1)/etc/config/media-downloader

	# Install system cron template job
	$(INSTALL_DIR) $(1)/etc/crontabs
	$(INSTALL_DATA) ./files/media-downloader.crontab $(1)/etc/crontabs/media-downloader

	# Install init.d service management file
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./files/media-downloader.init $(1)/etc/init.d/media-downloader
endef

$(eval $(call BuildPackage,media-downloader))
