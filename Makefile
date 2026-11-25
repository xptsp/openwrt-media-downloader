include $(TOPDIR)/rules.mk

PKG_NAME:=media-downloader
PKG_VERSION:=3.1
PKG_RELEASE:=2
PKG_MAINTAINER:=OpenWrt Developer

include $(INCLUDE_DIR)/package.mk

define Package/media-downloader
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Automated Media Downloader Web Control Panel
  DEPENDS:=+python3-light +python3-urllib +python3-codecs +python3-openssl +python3-cryptography +coreutils-sleep
  PKGARCH:=all
endef

define Package/media-downloader/description
  A lightweight background media sync downloader tool and web frontend interface 
  specifically engineered for capturing automated daily drama serial updates.
endef

define Package/media-downloader/postinst
#!/bin/sh
root="$${IPKG_INSTROOT}"
crontab="$$root/etc/crontabs/root"
job_file="$$root/usr/share/media-downloader/media-downloader.crontab"
mkdir -p "$$root/etc/crontabs" || exit 1
tmp=$$(mktemp "$$root/etc/crontabs/.media-downloader.XXXXXX") || exit 1

if [ -f "$$crontab" ]; then
    awk 'index($$0, "/usr/libexec/media-downloader/media-downloader.py --cron") == 0 && index($$0, "/usr/libexec/media-downloader.py --cron") == 0 { print }' "$$crontab" > "$$tmp" || {
        rm -f "$$tmp"
        exit 1
    }
fi

cat "$$job_file" >> "$$tmp" || {
    rm -f "$$tmp"
    exit 1
}
chmod 600 "$$tmp" && mv "$$tmp" "$$crontab" || {
    rm -f "$$tmp"
    exit 1
}

if [ -f "$$root/etc/crontabs/media-downloader" ] && grep -Fq '/usr/libexec/media-downloader.py --cron' "$$root/etc/crontabs/media-downloader"; then
    rm -f "$$root/etc/crontabs/media-downloader"
fi

if [ -z "$$root" ]; then
    if [ -x /etc/init.d/cron ]; then
        if ! /etc/init.d/cron restart; then
            logger -t media-downloader "Could not restart cron after installing the scheduled job."
        fi
    else
        logger -t media-downloader "Cron init script not found; scheduled job was installed but will not run."
    fi
fi
endef

define Package/media-downloader/conffiles
/etc/config/media-downloader
/usr/libexec/media-downloader/media-downloader.vapid
/usr/libexec/media-downloader/media-downloader.push
endef

define Build/Configure
	# Nothing to compile (Pure Script Stack)
endef

define Build/Compile
	# Nothing to compile (Pure Script Stack)
endef

define Package/media-downloader/install
	# Install background script engine
	$(INSTALL_DIR) $(1)/usr/libexec/media-downloader
	$(INSTALL_BIN) ./files/media-downloader.py $(1)/usr/libexec/media-downloader/media-downloader.py

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

	# Install cron job template; postinst merges it into root's crontab
	$(INSTALL_DATA) ./files/media-downloader.crontab $(1)/usr/share/media-downloader/media-downloader.crontab

	# Install init.d service management file
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./files/media-downloader.init $(1)/etc/init.d/media-downloader
endef

$(eval $(call BuildPackage,media-downloader))
