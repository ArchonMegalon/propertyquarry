GO ?= go
GO_ARCHIVE ?= $(PROPERTYQUARRY_GO_ARCHIVE)
OUTPUT_ROOT ?= ../../build/propertyquarry-release-control-v2/linux-amd64

.PHONY: build fmt repro stage-verify test

fmt:
	$(GO) fmt ./...

test:
	GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOWORK=off CGO_ENABLED=0 $(GO) test -mod=readonly ./...

build:
	PROPERTYQUARRY_GO_ARCHIVE="$(GO_ARCHIVE)" ./tools/build.sh "$(OUTPUT_ROOT)"

repro:
	PROPERTYQUARRY_GO_ARCHIVE="$(GO_ARCHIVE)" ./tools/repro-build.sh "$(OUTPUT_ROOT)"

stage-verify: repro
	./tools/stage-verify.sh "$(OUTPUT_ROOT)"
