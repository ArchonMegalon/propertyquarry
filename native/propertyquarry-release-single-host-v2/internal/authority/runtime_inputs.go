package authority

import (
	"encoding/json"
	"fmt"
	"strconv"
)

const maximumRuntimeInputBytes = 256 * 1024

type runtimeInputObservation struct {
	path   string
	digest string
	mode   int64
	uid    int64
	gid    int64
	size   int64
}

func validateSignedRuntimeInputs(preValue, postValue any) ([]runtimeInputObservation, []runtimeInputObservation, error) {
	pre, err := parseRuntimeInputObservations(preValue)
	if err != nil {
		return nil, nil, fmt.Errorf("pre-purge-runtime-inputs-invalid")
	}
	post, err := parseRuntimeInputObservations(postValue)
	if err != nil {
		return nil, nil, fmt.Errorf("runtime-inputs-invalid")
	}
	if len(pre) != len(post) || len(pre) != len(runtimeIsolationInputPaths) {
		return nil, nil, fmt.Errorf("runtime-input-transition-invalid")
	}
	for index := range pre {
		if index == 0 {
			if pre[index].path != BaseEnvironmentPath || post[index].path != BaseEnvironmentPath || pre[index].mode != post[index].mode || pre[index].uid != post[index].uid || pre[index].gid != post[index].gid || pre[index].digest == post[index].digest || post[index].size >= pre[index].size {
				return nil, nil, fmt.Errorf("runtime-input-root-transition-invalid")
			}
			continue
		}
		if pre[index] != post[index] {
			return nil, nil, fmt.Errorf("runtime-input-transition-invalid")
		}
	}
	return pre, post, nil
}

func parseRuntimeInputObservations(value any) ([]runtimeInputObservation, error) {
	items, ok := value.([]any)
	if !ok || len(items) != len(runtimeIsolationInputPaths) {
		return nil, fmt.Errorf("runtime-inputs-shape-invalid")
	}
	observations := make([]runtimeInputObservation, len(items))
	for index, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok || !hasKeys(item, "gid", "mode", "path", "sha256", "size", "uid") {
			return nil, fmt.Errorf("runtime-input-shape-invalid")
		}
		path, pathOK := exactString(item["path"])
		digestValue, digestOK := exactString(item["sha256"])
		mode, modeOK := exactInt(item["mode"], 384, 384)
		uid, uidOK := exactInt(item["uid"], 1000, 1000)
		gid, gidOK := exactInt(item["gid"], 1000, 1000)
		size, sizeOK := exactInt(item["size"], 1, maximumRuntimeInputBytes)
		if !pathOK || path != runtimeIsolationInputPaths[index] || !digestOK || !digestPattern.MatchString(digestValue) || !modeOK || mode != 384 || !uidOK || uid != 1000 || !gidOK || gid != 1000 || !sizeOK {
			return nil, fmt.Errorf("runtime-input-binding-invalid")
		}
		observations[index] = runtimeInputObservation{path: path, digest: digestValue, mode: mode, uid: uid, gid: gid, size: size}
	}
	return observations, nil
}

func runtimeInputObservationsEqual(value any, expected []runtimeInputObservation) bool {
	actual, err := parseRuntimeInputObservations(value)
	if err != nil || len(actual) != len(expected) {
		return false
	}
	for index := range expected {
		if actual[index] != expected[index] {
			return false
		}
	}
	return true
}

func runtimeInputObservationValues(observations []runtimeInputObservation) []any {
	values := make([]any, len(observations))
	for index, observation := range observations {
		values[index] = map[string]any{
			"gid":    json.Number(strconv.FormatInt(observation.gid, 10)),
			"mode":   json.Number(strconv.FormatInt(observation.mode, 10)),
			"path":   observation.path,
			"sha256": observation.digest,
			"size":   json.Number(strconv.FormatInt(observation.size, 10)),
			"uid":    json.Number(strconv.FormatInt(observation.uid, 10)),
		}
	}
	return values
}

func validateCurrentRuntimeInputs(root string, pre, post []runtimeInputObservation) error {
	if len(pre) != len(runtimeIsolationInputPaths) || len(post) != len(runtimeIsolationInputPaths) {
		return fmt.Errorf("runtime-input-count-invalid")
	}
	for index, path := range runtimeIsolationInputPaths {
		if err := validateExternalParentChain(root, rooted(root, path), 1000, 1000); err != nil {
			return fmt.Errorf("runtime-input-parent-invalid")
		}
		raw, err := readSecureFile(rooted(root, path), 0o600, 1000, 1000, maximumRuntimeInputBytes)
		if err != nil {
			return fmt.Errorf("runtime-input-unavailable")
		}
		actual := runtimeInputObservation{path: path, digest: digest(raw), mode: 384, uid: 1000, gid: 1000, size: int64(len(raw))}
		zero(raw)
		if index == 0 {
			if actual != pre[index] && actual != post[index] {
				return fmt.Errorf("runtime-input-root-state-invalid")
			}
			continue
		}
		if actual != pre[index] || actual != post[index] {
			return fmt.Errorf("runtime-input-state-invalid")
		}
	}
	return nil
}

func validateExactCurrentRuntimeInputs(root string, expected []runtimeInputObservation) error {
	if len(expected) != len(runtimeIsolationInputPaths) {
		return fmt.Errorf("runtime-input-count-invalid")
	}
	for index, path := range runtimeIsolationInputPaths {
		observation := expected[index]
		if observation.path != path || errRuntimeInputParent(root, path) != nil {
			return fmt.Errorf("runtime-input-parent-invalid")
		}
		raw, err := readSecureFile(rooted(root, path), 0o600, 1000, 1000, maximumRuntimeInputBytes)
		if err != nil {
			return fmt.Errorf("runtime-input-unavailable")
		}
		matches := digest(raw) == observation.digest && int64(len(raw)) == observation.size
		zero(raw)
		if !matches {
			return fmt.Errorf("runtime-input-state-invalid")
		}
	}
	return nil
}

func errRuntimeInputParent(root, path string) error {
	return validateExternalParentChain(root, rooted(root, path), 1000, 1000)
}
