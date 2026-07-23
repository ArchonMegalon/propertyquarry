package main

import (
	"os"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

func main() {
	os.Exit(authority.Run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}
