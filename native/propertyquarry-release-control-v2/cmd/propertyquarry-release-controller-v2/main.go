package main

import (
	"os"

	"propertyquarry.local/release-control-v2/internal/releasecontrol"
)

func main() {
	os.Exit(releasecontrol.Run(
		releasecontrol.Controller,
		os.Args[1:],
		os.Stdout,
		os.Stderr,
	))
}
