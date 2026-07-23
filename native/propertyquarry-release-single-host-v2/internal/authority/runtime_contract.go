package authority

import (
	"fmt"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
)

const (
	RuntimeRetirementStepID       = "retire-stale-propertyquarry-runtime"
	RuntimeDeployExecutablePath   = "/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-deploy-v2"
	RuntimeDeployReceiptDirectory = "/var/lib/propertyquarry-release-single-host-v2/deploy-receipts"
	DockerExecutablePath          = "/usr/bin/docker"
	DockerComposePluginPath       = "/usr/libexec/docker/cli-plugins/docker-compose"
	PropertyComposePath           = "/docker/property/docker-compose.property.yml"
	CloudflaredComposePath        = "/docker/property/docker-compose.cloudflared.yml"
)

var desiredRuntimeContainerAllowlist = []string{
	"propertyquarry-api-live",
	"propertyquarry-cloudflared-live",
	"propertyquarry-db-live",
	"propertyquarry-migrate-live",
	"propertyquarry-render-live",
	"propertyquarry-scheduler-live",
	"propertyquarry-worker-live",
}

var (
	runtimeContainerNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	runtimeContainerIDPattern   = regexp.MustCompile(`^[0-9a-f]{64}$`)
	runtimeCreatedAtPattern     = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?$`)
	runtimeVolumeCreatedPattern = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+ -]+Z?$`)
	legacyRuntimeNamePatterns   = []*regexp.Regexp{
		regexp.MustCompile(`^propertyquarry-(?:api|cloudflared|migrate|render-tools|scheduler|worker)$`),
		regexp.MustCompile(`^propertyquarry-(?:api|db|migrate|render)-[0-9a-f]{8}$`),
		regexp.MustCompile(`^propertyquarry-admission-audit-[0-9a-f]{8}$`),
		regexp.MustCompile(`^propertyquarry-release-pin-(?:render|web)-[0-9]+$`),
		regexp.MustCompile(`^pq-ai-panorama-(?:canonical-strict|prater-preflight)$`),
	}
)

func validateRuntimeRetirementContract(value any, runtimeSHA, deploymentID string) (map[string]any, string, error) {
	contract, ok := value.(map[string]any)
	if !ok || !hasKeys(contract, "containers", "deployment_id", "desired_live_allowlist", "operation", "preserve_volumes", "receipt_path") || contract["operation"] != operationRetireStaleRuntime || contract["deployment_id"] != deploymentID || contract["preserve_volumes"] != true {
		return nil, "", fmt.Errorf("runtime-retirement-contract-invalid")
	}
	expectedReceipt := filepath.Join(runtimeIsolationReceiptDirectory, runtimeSHA, deploymentID, operationRetireStaleRuntime+".json")
	if contract["receipt_path"] != expectedReceipt || !exactOrderedStringArray(contract["desired_live_allowlist"], desiredRuntimeContainerAllowlist) {
		return nil, "", fmt.Errorf("runtime-retirement-contract-invalid")
	}
	containers, ok := contract["containers"].([]any)
	if !ok || len(containers) > 64 {
		return nil, "", fmt.Errorf("runtime-retirement-containers-invalid")
	}
	seen := make(map[string]struct{}, len(containers))
	previousName := ""
	for _, item := range containers {
		container, ok := item.(map[string]any)
		if !ok || !hasKeys(container, "compose_project", "compose_service", "container_id", "created_at", "image", "image_id", "mounts", "name", "networks") {
			return nil, "", fmt.Errorf("runtime-retirement-container-invalid")
		}
		name, nameOK := exactString(container["name"])
		containerID, containerIDOK := exactString(container["container_id"])
		createdAt, createdAtOK := exactString(container["created_at"])
		image, imageOK := exactString(container["image"])
		imageID, imageIDOK := exactString(container["image_id"])
		project, projectOK := container["compose_project"].(string)
		service, serviceOK := container["compose_service"].(string)
		if !nameOK || !runtimeContainerNamePattern.MatchString(name) || name <= previousName || containsString(desiredRuntimeContainerAllowlist, name) || !matchesLegacyRuntimeName(name) || !containerIDOK || !runtimeContainerIDPattern.MatchString(containerID) || !createdAtOK || !runtimeCreatedAtPattern.MatchString(createdAt) || !imageOK || len(image) > 512 || !imageIDOK || !digestPattern.MatchString(imageID) || !projectOK || len(project) > 255 || containsForbiddenArgumentByte(project) || !serviceOK || len(service) > 255 || containsForbiddenArgumentByte(service) {
			return nil, "", fmt.Errorf("runtime-retirement-container-invalid")
		}
		if _, duplicate := seen[name]; duplicate {
			return nil, "", fmt.Errorf("runtime-retirement-container-invalid")
		}
		seen[name], previousName = struct{}{}, name
		if !sortedUniqueStrings(container["networks"], 255) || !validRetirementMounts(container["mounts"]) {
			return nil, "", fmt.Errorf("runtime-retirement-container-invalid")
		}
	}
	raw, err := canonicalJSON(contract)
	if err != nil {
		return nil, "", fmt.Errorf("runtime-retirement-contract-invalid")
	}
	return contract, digest(raw), nil
}

func validRetirementMounts(value any) bool {
	items, ok := value.([]any)
	if !ok || len(items) > 64 {
		return false
	}
	var previous []byte
	for _, item := range items {
		mount, ok := item.(map[string]any)
		if !ok || !hasKeys(mount, "destination", "driver", "mode", "name", "propagation", "rw", "source", "type") {
			return false
		}
		destination, destinationOK := exactString(mount["destination"])
		driver, driverOK := mount["driver"].(string)
		mode, modeOK := mount["mode"].(string)
		name, nameOK := mount["name"].(string)
		propagation, propagationOK := mount["propagation"].(string)
		source, sourceOK := mount["source"].(string)
		kind, kindOK := exactString(mount["type"])
		_, rwOK := mount["rw"].(bool)
		if !destinationOK || !filepath.IsAbs(destination) || !driverOK || !modeOK || !nameOK || !propagationOK || !sourceOK || !kindOK || !rwOK || (kind != "bind" && kind != "tmpfs" && kind != "volume") || (kind == "volume" && (name == "" || source != name)) || (kind != "volume" && name != "") || (kind != "volume" && !filepath.IsAbs(source)) {
			return false
		}
		for _, text := range []string{destination, driver, mode, name, propagation, source, kind} {
			if len(text) > 4096 || containsForbiddenArgumentByte(text) {
				return false
			}
		}
		raw, err := canonicalJSON(mount)
		if err != nil || (previous != nil && string(raw) <= string(previous)) {
			zero(raw)
			return false
		}
		zero(previous)
		previous = raw
	}
	zero(previous)
	return true
}

func validateRuntimeDeployContract(value any, runtimeSHA, deploymentID string) (map[string]any, string, error) {
	contract, ok := value.(map[string]any)
	if !ok || !hasKeys(contract, "compose_argv", "compose_files", "compose_plugin", "deployment_id", "docker_executable", "env_files", "operation", "receipt_path") || contract["operation"] != "deploy-runtime" || contract["deployment_id"] != deploymentID {
		return nil, "", fmt.Errorf("runtime-deploy-contract-invalid")
	}
	expectedReceipt := filepath.Join(RuntimeDeployReceiptDirectory, runtimeSHA, deploymentID, "deploy-runtime.json")
	if contract["receipt_path"] != expectedReceipt || !exactOrderedStringArray(contract["env_files"], runtimeIsolationInputPaths) || !exactOrderedStringArray(contract["compose_argv"], expectedComposeArgv()) {
		return nil, "", fmt.Errorf("runtime-deploy-contract-invalid")
	}
	if !validRuntimeFileObservation(contract["docker_executable"], DockerExecutablePath, true) || !validRuntimeFileObservation(contract["compose_plugin"], DockerComposePluginPath, true) {
		return nil, "", fmt.Errorf("runtime-deploy-executable-invalid")
	}
	composeFiles, ok := contract["compose_files"].([]any)
	if !ok || len(composeFiles) != 2 || !validRuntimeFileObservation(composeFiles[0], PropertyComposePath, false) || !validRuntimeFileObservation(composeFiles[1], CloudflaredComposePath, false) {
		return nil, "", fmt.Errorf("runtime-deploy-compose-files-invalid")
	}
	raw, err := canonicalJSON(contract)
	if err != nil {
		return nil, "", fmt.Errorf("runtime-deploy-contract-invalid")
	}
	return contract, digest(raw), nil
}

func validRuntimeFileObservation(value any, expectedPath string, executable bool) bool {
	item, ok := value.(map[string]any)
	if !ok || !hasKeys(item, "gid", "mode", "path", "sha256", "size", "uid") || item["path"] != expectedPath {
		return false
	}
	digestValue, digestOK := exactString(item["sha256"])
	mode, modeOK := exactString(item["mode"])
	uid, uidOK := exactInt(item["uid"], 0, 1<<31-1)
	gid, gidOK := exactInt(item["gid"], 0, 1<<31-1)
	_, sizeOK := exactInt(item["size"], 1, 256*1024*1024)
	if !digestOK || !digestPattern.MatchString(digestValue) || !modeOK || !regexp.MustCompile(`^0[4567][0-7]{2}$`).MatchString(mode) || !uidOK || !gidOK || !sizeOK {
		return false
	}
	if executable {
		return mode == "0755" && uid == 0 && gid == 0
	}
	return (mode == "0400" || mode == "0444" || mode == "0600" || mode == "0644") && (uid == 0 || uid == 1000) && (gid == 0 || gid == 1000)
}

func validateCurrentRuntimeFileObservation(root string, value any) error {
	item, ok := value.(map[string]any)
	if !ok || !hasKeys(item, "gid", "mode", "path", "sha256", "size", "uid") {
		return fmt.Errorf("runtime-file-observation-invalid")
	}
	path, pathOK := exactString(item["path"])
	digestValue, digestOK := exactString(item["sha256"])
	modeText, modeOK := exactString(item["mode"])
	uid, uidOK := exactInt(item["uid"], 0, 1<<31-1)
	gid, gidOK := exactInt(item["gid"], 0, 1<<31-1)
	size, sizeOK := exactInt(item["size"], 1, 256*1024*1024)
	mode, modeErr := strconv.ParseUint(modeText, 8, 32)
	if !pathOK || !filepath.IsAbs(path) || !digestOK || !digestPattern.MatchString(digestValue) || !modeOK || modeErr != nil || !uidOK || !gidOK || !sizeOK {
		return fmt.Errorf("runtime-file-observation-invalid")
	}
	actualUID, actualGID := uint32(uid), uint32(gid)
	if root != "" && root != "/" && uid == 0 && gid == 0 {
		actualUID, actualGID = secureOwner(root)
	}
	actualPath := rooted(root, path)
	if err := validateExternalParentChain(root, actualPath, actualUID, actualGID); err != nil {
		return fmt.Errorf("runtime-file-parent-invalid")
	}
	raw, err := readSecureFile(actualPath, uint32(mode), actualUID, actualGID, 256*1024*1024)
	if err != nil {
		return fmt.Errorf("runtime-file-unavailable")
	}
	defer zero(raw)
	if int64(len(raw)) != size || digest(raw) != digestValue {
		return fmt.Errorf("runtime-file-content-invalid")
	}
	return nil
}

func expectedComposeArgv() []string {
	return []string{
		DockerExecutablePath, "compose", "--ansi", "never", "--progress", "quiet", "--project-name", "property", "--project-directory", "/docker/property",
		"--env-file", BaseEnvironmentPath, "--env-file", SceneVideoEnvPath, "--env-file", DatabaseRuntimeEnvironmentPath,
		"--env-file", AdmissionEnvPath, "--env-file", GoogleIdentityEnvPath, "--env-file", RegistrationEmailEnvPath,
		"--file", PropertyComposePath, "--file", CloudflaredComposePath,
		"up", "--detach", "--pull", "always", "--quiet-pull", "--no-build", "--timeout", "120", "--wait", "--wait-timeout", "900",
	}
}

func sortedUniqueStrings(value any, maximumLength int) bool {
	items, ok := value.([]any)
	if !ok || len(items) > 64 {
		return false
	}
	strings := make([]string, len(items))
	for index, item := range items {
		text, ok := exactString(item)
		if !ok || len(text) > maximumLength || containsForbiddenArgumentByte(text) {
			return false
		}
		strings[index] = text
	}
	if !sort.StringsAreSorted(strings) {
		return false
	}
	for index := 1; index < len(strings); index++ {
		if strings[index] == strings[index-1] {
			return false
		}
	}
	return true
}

func containsString(items []string, expected string) bool {
	for _, item := range items {
		if item == expected {
			return true
		}
	}
	return false
}

func matchesLegacyRuntimeName(name string) bool {
	for _, pattern := range legacyRuntimeNamePatterns {
		if pattern.MatchString(name) {
			return true
		}
	}
	return false
}

func exactOrderedStringArray(value any, expected []string) bool {
	items, ok := value.([]any)
	if !ok || len(items) != len(expected) {
		return false
	}
	for index, item := range items {
		text, ok := exactString(item)
		if !ok || text != expected[index] {
			return false
		}
	}
	return true
}
