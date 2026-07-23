package authority

import (
	"fmt"
	"regexp"
)

const (
	databasePGDataVolumeName       = "property_propertyquarry_pgdata"
	databasePGDataVolumeMountpoint = "/var/lib/docker/volumes/property_propertyquarry_pgdata/_data"
)

type databaseSubstrate struct {
	value        map[string]any
	digest       string
	containerID  string
	databaseOID  int64
	imageID      string
	repoDigest   string
	pgdataVolume map[string]any
}

func validateDatabaseSubstrate(value any, databaseImage string) (*databaseSubstrate, error) {
	item, ok := value.(map[string]any)
	if !ok || !hasKeys(item, "container_id", "container_name", "database", "database_oid", "image", "image_id", "pgdata_volume", "repo_digest") {
		return nil, fmt.Errorf("database-substrate-shape-invalid")
	}
	containerID, containerIDOK := exactString(item["container_id"])
	imageID, imageIDOK := exactString(item["image_id"])
	repoDigest, repoDigestOK := exactString(item["repo_digest"])
	databaseOID, databaseOIDOK := exactInt(item["database_oid"], 1, 1<<62)
	volume, volumeOK := item["pgdata_volume"].(map[string]any)
	if !containerIDOK || !runtimeContainerIDPattern.MatchString(containerID) || item["container_name"] != databaseControlContainer || item["database"] != databaseControlDatabase || item["image"] != databaseImage || !imageIDOK || !digestPattern.MatchString(imageID) || !repoDigestOK || repoDigest != canonicalRepoDigest(databaseImage) || !databaseOIDOK || !volumeOK || !validDatabasePGDataVolume(volume) {
		return nil, fmt.Errorf("database-substrate-binding-invalid")
	}
	raw, err := canonicalJSON(item)
	if err != nil {
		return nil, fmt.Errorf("database-substrate-canonical-invalid")
	}
	digestValue := digest(raw)
	zero(raw)
	return &databaseSubstrate{value: item, digest: digestValue, containerID: containerID, databaseOID: databaseOID, imageID: imageID, repoDigest: repoDigest, pgdataVolume: volume}, nil
}

func validDatabasePGDataVolume(value map[string]any) bool {
	if !hasKeys(value, "created_at", "driver", "labels", "mountpoint", "name", "options", "scope") || value["driver"] != "local" || value["mountpoint"] != databasePGDataVolumeMountpoint || value["name"] != databasePGDataVolumeName || value["scope"] != "local" {
		return false
	}
	createdAt, ok := exactString(value["created_at"])
	if !ok || !runtimeVolumeCreatedPattern.MatchString(createdAt) {
		return false
	}
	labels, labelsOK := value["labels"].(map[string]any)
	options, optionsOK := value["options"].(map[string]any)
	return labelsOK && optionsOK && validStringMap(labels, 64) && len(options) == 0 &&
		labels["com.docker.compose.project"] == ProjectName &&
		labels["com.docker.compose.volume"] == "propertyquarry_pgdata"
}

func validStringMap(value map[string]any, maximumEntries int) bool {
	if len(value) > maximumEntries {
		return false
	}
	namePattern := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$`)
	for key, raw := range value {
		text, ok := raw.(string)
		if !namePattern.MatchString(key) || !ok || len(text) > 4096 || containsForbiddenArgumentByte(text) {
			return false
		}
	}
	return true
}

func databaseSubstrateValueEqual(value any, expected *databaseSubstrate) bool {
	if expected == nil {
		return false
	}
	actual, err := validateDatabaseSubstrate(value, stringValue(expected.value["image"]))
	return err == nil && actual.digest == expected.digest
}
