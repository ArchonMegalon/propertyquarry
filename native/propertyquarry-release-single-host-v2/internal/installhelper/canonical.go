package installhelper

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"sort"
)

var (
	digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	shaPattern    = regexp.MustCompile(`^[0-9a-f]{40}$`)
	modePattern   = regexp.MustCompile(`^0[0-7]{3}$`)
)

func strictJSON(raw []byte, maximum int) (map[string]any, error) {
	if len(raw) == 0 || len(raw) > maximum {
		return nil, fmt.Errorf("json-size-invalid")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := decodeJSONValue(decoder, 0)
	if err != nil {
		return nil, err
	}
	if err := rejectJSONTail(decoder); err != nil {
		return nil, err
	}
	object, ok := value.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("json-object-required")
	}
	canonical, err := canonicalJSON(object)
	if err != nil || !bytes.Equal(canonical, raw) {
		zero(canonical)
		return nil, fmt.Errorf("json-not-canonical")
	}
	return object, nil
}

func decodeJSONValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > 32 {
		return nil, fmt.Errorf("json-depth-invalid")
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, fmt.Errorf("json-invalid")
	}
	switch item := token.(type) {
	case json.Delim:
		switch item {
		case '{':
			object := map[string]any{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return nil, fmt.Errorf("json-invalid")
				}
				key, ok := keyToken.(string)
				if !ok {
					return nil, fmt.Errorf("json-key-invalid")
				}
				if _, duplicate := object[key]; duplicate {
					return nil, fmt.Errorf("json-duplicate-key")
				}
				child, err := decodeJSONValue(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				object[key] = child
			}
			closing, err := decoder.Token()
			if err != nil || closing != json.Delim('}') {
				return nil, fmt.Errorf("json-object-unclosed")
			}
			return object, nil
		case '[':
			array := []any{}
			for decoder.More() {
				child, err := decodeJSONValue(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				array = append(array, child)
			}
			closing, err := decoder.Token()
			if err != nil || closing != json.Delim(']') {
				return nil, fmt.Errorf("json-array-unclosed")
			}
			return array, nil
		default:
			return nil, fmt.Errorf("json-delimiter-invalid")
		}
	case string, bool, nil, json.Number:
		return item, nil
	default:
		return nil, fmt.Errorf("json-type-invalid")
	}
}

func rejectJSONTail(decoder *json.Decoder) error {
	var tail any
	if err := decoder.Decode(&tail); err != io.EOF {
		return fmt.Errorf("json-trailing-data")
	}
	return nil
}

func canonicalJSON(value any) ([]byte, error) {
	var output bytes.Buffer
	if err := writeCanonical(&output, value, 0); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

func writeCanonical(output *bytes.Buffer, value any, depth int) error {
	if depth > 32 {
		return fmt.Errorf("json-depth-invalid")
	}
	switch item := value.(type) {
	case nil:
		output.WriteString("null")
	case bool:
		if item {
			output.WriteString("true")
		} else {
			output.WriteString("false")
		}
	case string:
		encoded, err := json.Marshal(item)
		if err != nil {
			return err
		}
		output.Write(encoded)
	case json.Number:
		text := item.String()
		if !regexp.MustCompile(`^(0|[1-9][0-9]*)$`).MatchString(text) {
			return fmt.Errorf("json-number-invalid")
		}
		output.WriteString(text)
	case []any:
		output.WriteByte('[')
		for index, child := range item {
			if index > 0 {
				output.WriteByte(',')
			}
			if err := writeCanonical(output, child, depth+1); err != nil {
				return err
			}
		}
		output.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(item))
		for key := range item {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		output.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				output.WriteByte(',')
			}
			encoded, err := json.Marshal(key)
			if err != nil {
				return err
			}
			output.Write(encoded)
			output.WriteByte(':')
			if err := writeCanonical(output, item[key], depth+1); err != nil {
				return err
			}
		}
		output.WriteByte('}')
	default:
		return fmt.Errorf("json-type-invalid")
	}
	return nil
}

func hasKeys(value map[string]any, keys ...string) bool {
	if len(value) != len(keys) {
		return false
	}
	for _, key := range keys {
		if _, ok := value[key]; !ok {
			return false
		}
	}
	return true
}

func exactString(value any) (string, bool) {
	text, ok := value.(string)
	return text, ok && text != ""
}

func exactInt(value any, minimum, maximum int64) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	parsed, err := number.Int64()
	return parsed, err == nil && parsed >= minimum && parsed <= maximum
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func framed(domain string, raw []byte) []byte {
	result := make([]byte, 0, len(domain)+8+len(raw))
	result = append(result, []byte(domain)...)
	length := uint64(len(raw))
	for shift := 56; shift >= 0; shift -= 8 {
		result = append(result, byte(length>>shift))
	}
	return append(result, raw...)
}

func zero(raw []byte) {
	for index := range raw {
		raw[index] = 0
	}
}
