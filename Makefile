# -----------------------------------------------------------------------------
# promlens -- Podman container build & export
# -----------------------------------------------------------------------------

VERSION    := 0.22.4
GIT_COMMIT := $(shell git rev-parse --short HEAD)

IMAGE_NAME := promlens
IMAGE_REF  := $(IMAGE_NAME):$(VERSION)
ARCHIVE    := $(IMAGE_NAME)-$(VERSION).tar

DEB_VER    := $(VERSION)+git$(GIT_COMMIT)
DEB_ARCH   := all
DEB_NAME   := $(IMAGE_NAME)_$(DEB_VER)_$(DEB_ARCH)
MAINTAINER := tiix <contact@tiix.wtf>

# Config files to mount (can be overridden)
DATA_DIR      ?= $(CURDIR)/examples/data
BIND_HOST     ?= 127.0.0.1
BIND_PORT     ?= 8001

.DEFAULT_GOAL := help

.PHONY: help build export load run deb clean

## help   : Show this help message
help:
	@grep -E '^## ' Makefile | sed 's/^## /  /'

## build  : Build the container image
build:
	podman build \
		--file Containerfile \
		--tag $(IMAGE_REF) \
		.

## export : Build then save the image to $(ARCHIVE)
export: build
	podman save \
		--output $(ARCHIVE) \
		$(IMAGE_REF)
	@echo "Image saved to $(ARCHIVE)"
	@echo "Load with: make load  or  podman load -i $(ARCHIVE)"

## load   : Load a previously exported archive into Podman
load:
	podman load -i $(ARCHIVE)

## run    : Run ProMLens (mounts DATA_DIR for config files)
run: build
	exec podman run --rm -it \
		--name promlens \
		-p $(BIND_HOST):$(BIND_PORT):8000 \
		-v $(DATA_DIR):/data:z \
		$(IMAGE_REF)

## deb    : Build the Debian package (output: ./$(DEB_NAME).deb)
deb:
	printf '$(IMAGE_NAME) ($(DEB_VER)) trixie; urgency=medium\n\n  * Build from git commit $(GIT_COMMIT)\n\n -- $(MAINTAINER)  $(shell date -R)\n' > debian/changelog
	chmod +x debian/rules
	dpkg-buildpackage -us -uc -b
	mv ../$(DEB_NAME).deb ../$(IMAGE_NAME)_$(DEB_VER)_*.buildinfo ../$(IMAGE_NAME)_$(DEB_VER)_*.changes .

## clean  : Remove the container image, exported archive, and debian build files
clean:
	-podman rmi $(IMAGE_REF)
	-rm -f $(ARCHIVE)
	-dh_clean 2>/dev/null || true
	-rm -f *.deb *.buildinfo *.changes
