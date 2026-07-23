package main

import (
	"os"

	"propertyquarry.local/release-single-host-v2/internal/installhelper"
)

func main() {
	os.Exit(installhelper.Run(os.Args[1:], os.Stdout, os.Stderr))
}
