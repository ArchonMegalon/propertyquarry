package releasecontrol

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"sort"
	"strconv"
	"unicode/utf8"
)

const (
	requestSchema       = "propertyquarry.release-request.v2"
	maxRequestBytes     = 1_048_576
	maxRequestJSONDepth = 32
)

var (
	requestIdentifierPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,255}\z`)
	requestSHA1Pattern       = regexp.MustCompile(`^[0-9a-f]{40}\z`)
	requestDigestPattern     = regexp.MustCompile(`^sha256:[0-9a-f]{64}\z`)
	errRequestInvalid        = errors.New("release request invalid")
)

type quarantinedIdentity struct {
	Audience     string
	Repository   string
	Ref          string
	CandidateSHA string
	WorkflowRef  string
	WorkflowSHA  string
	RunID        string
	RunAttempt   int64
	Job          string
	Environment  string
}

type quarantinedEnvelope struct {
	Operation string
	RequestID string
	Nonce     string
	IssuedAt  int64
	ExpiresAt int64
	Identity  quarantinedIdentity
}

// quarantinedRequest is a syntax-only intake result. In particular,
// authenticationEstablished is never derived from a digest comparison. The
// broker must release these buffers without producing a response or effect.
type quarantinedRequest struct {
	rawBody                   []byte
	rawBodyDigest             string
	canonicalBody             []byte
	canonicalBodyDigest       string
	canonicalEnvelope         []byte
	canonicalEnvelopeDigest   string
	claimedEnvelopeDigest     string
	envelopeDigestMatches     bool
	signaturePayload          []byte
	signaturePayloadDigest    string
	requestSignature          string
	envelope                  quarantinedEnvelope
	authenticationEstablished bool
	authenticatedKeyID        string
}

func (request *quarantinedRequest) release() {
	if request == nil {
		return
	}
	zero(request.rawBody)
	zero(request.canonicalBody)
	zero(request.canonicalEnvelope)
	zero(request.signaturePayload)
	request.rawBody = nil
	request.rawBodyDigest = ""
	request.canonicalBody = nil
	request.canonicalBodyDigest = ""
	request.canonicalEnvelope = nil
	request.canonicalEnvelopeDigest = ""
	request.claimedEnvelopeDigest = ""
	request.envelopeDigestMatches = false
	request.signaturePayload = nil
	request.signaturePayloadDigest = ""
	request.requestSignature = ""
	request.envelope = quarantinedEnvelope{}
	request.authenticationEstablished = false
	request.authenticatedKeyID = ""
}

func parseQuarantinedRequest(raw []byte) (*quarantinedRequest, error) {
	if len(raw) < 1 || len(raw) > maxRequestBytes {
		return nil, errRequestInvalid
	}
	if err := validateRequestUnicode(raw); err != nil {
		return nil, errRequestInvalid
	}
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return nil, errRequestInvalid
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(outer, "schema", "envelope", "envelope_digest", "request_signature") {
		return nil, errRequestInvalid
	}
	schema, ok := exactString(outer["schema"])
	if !ok || schema != requestSchema {
		return nil, errRequestInvalid
	}
	requestSignature, ok := exactString(outer["request_signature"])
	if !ok || requestSignature == "" {
		return nil, errRequestInvalid
	}
	claimedEnvelopeDigest, ok := exactString(outer["envelope_digest"])
	if !ok || !requestDigestPattern.MatchString(claimedEnvelopeDigest) {
		return nil, errRequestInvalid
	}

	envelopeObject, ok := outer["envelope"].(map[string]any)
	if !ok || !hasExactKeys(
		envelopeObject,
		"operation",
		"request_id",
		"nonce",
		"issued_at",
		"expires_at",
		"identity",
	) {
		return nil, errRequestInvalid
	}
	operation, ok := exactString(envelopeObject["operation"])
	if !ok || (operation != "release-preflight" && operation != "release-run") {
		return nil, errRequestInvalid
	}
	requestID, ok := validRequestIdentifier(envelopeObject["request_id"])
	if !ok {
		return nil, errRequestInvalid
	}
	nonce, ok := validRequestIdentifier(envelopeObject["nonce"])
	if !ok {
		return nil, errRequestInvalid
	}
	issuedAt, ok := exactBoundedInt(envelopeObject["issued_at"], 0)
	if !ok {
		return nil, errRequestInvalid
	}
	expiresAt, ok := exactBoundedInt(envelopeObject["expires_at"], 0)
	if !ok {
		return nil, errRequestInvalid
	}
	identityObject, ok := envelopeObject["identity"].(map[string]any)
	if !ok || !hasExactKeys(
		identityObject,
		"audience",
		"repository",
		"ref",
		"candidate_sha",
		"workflow_ref",
		"workflow_sha",
		"run_id",
		"run_attempt",
		"job",
		"environment",
	) {
		return nil, errRequestInvalid
	}
	identity, ok := parseQuarantinedIdentity(identityObject)
	if !ok {
		return nil, errRequestInvalid
	}

	canonicalEnvelope, err := canonicalJSON(envelopeObject)
	if err != nil {
		return nil, errRequestInvalid
	}
	canonicalBody, err := canonicalJSON(outer)
	if err != nil {
		zero(canonicalEnvelope)
		return nil, errRequestInvalid
	}
	signaturePayload, err := canonicalJSON(map[string]any{
		"schema":          schema,
		"envelope":        envelopeObject,
		"envelope_digest": claimedEnvelopeDigest,
	})
	if err != nil {
		zero(canonicalEnvelope)
		zero(canonicalBody)
		return nil, errRequestInvalid
	}
	rawCopy := append([]byte(nil), raw...)
	canonicalEnvelopeDigest := sha256Digest(canonicalEnvelope)
	return &quarantinedRequest{
		rawBody:                 rawCopy,
		rawBodyDigest:           sha256Digest(rawCopy),
		canonicalBody:           canonicalBody,
		canonicalBodyDigest:     sha256Digest(canonicalBody),
		canonicalEnvelope:       canonicalEnvelope,
		canonicalEnvelopeDigest: canonicalEnvelopeDigest,
		claimedEnvelopeDigest:   claimedEnvelopeDigest,
		envelopeDigestMatches:   claimedEnvelopeDigest == canonicalEnvelopeDigest,
		signaturePayload:        signaturePayload,
		signaturePayloadDigest:  sha256Digest(signaturePayload),
		requestSignature:        requestSignature,
		envelope: quarantinedEnvelope{
			Operation: operation,
			RequestID: requestID,
			Nonce:     nonce,
			IssuedAt:  issuedAt,
			ExpiresAt: expiresAt,
			Identity:  identity,
		},
		authenticationEstablished: false,
	}, nil
}

func parseQuarantinedIdentity(value map[string]any) (quarantinedIdentity, bool) {
	audience, ok := validRequestIdentifier(value["audience"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	repository, ok := validRequestIdentifier(value["repository"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	ref, ok := validRequestIdentifier(value["ref"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	workflowRef, ok := validRequestIdentifier(value["workflow_ref"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	runID, ok := validRequestIdentifier(value["run_id"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	job, ok := validRequestIdentifier(value["job"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	environment, ok := validRequestIdentifier(value["environment"])
	if !ok {
		return quarantinedIdentity{}, false
	}
	candidateSHA, ok := exactString(value["candidate_sha"])
	if !ok || !requestSHA1Pattern.MatchString(candidateSHA) {
		return quarantinedIdentity{}, false
	}
	workflowSHA, ok := exactString(value["workflow_sha"])
	if !ok || !requestSHA1Pattern.MatchString(workflowSHA) {
		return quarantinedIdentity{}, false
	}
	runAttempt, ok := exactBoundedInt(value["run_attempt"], 1)
	if !ok {
		return quarantinedIdentity{}, false
	}
	return quarantinedIdentity{
		Audience:     audience,
		Repository:   repository,
		Ref:          ref,
		CandidateSHA: candidateSHA,
		WorkflowRef:  workflowRef,
		WorkflowSHA:  workflowSHA,
		RunID:        runID,
		RunAttempt:   runAttempt,
		Job:          job,
		Environment:  environment,
	}, true
}

func exactString(value any) (string, bool) {
	result, ok := value.(string)
	return result, ok
}

func validRequestIdentifier(value any) (string, bool) {
	result, ok := exactString(value)
	return result, ok && requestIdentifierPattern.MatchString(result)
}

func exactBoundedInt(value any, minimum int64) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil || parsed < minimum {
		return 0, false
	}
	return parsed, true
}

func hasExactKeys(value map[string]any, keys ...string) bool {
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

func decodeStrictJSON(raw []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := decodeStrictJSONValue(decoder, 0)
	if err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, errRequestInvalid
	}
	return value, nil
}

func decodeStrictJSONValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > maxRequestJSONDepth {
		return nil, errRequestInvalid
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, errRequestInvalid
	}
	delimiter, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		switch token.(type) {
		case nil, bool, string, json.Number:
			return token, nil
		default:
			return nil, errRequestInvalid
		}
	}
	switch delimiter {
	case '{':
		object := make(map[string]any)
		for decoder.More() {
			if depth+1 > maxRequestJSONDepth {
				return nil, errRequestInvalid
			}
			keyToken, err := decoder.Token()
			if err != nil {
				return nil, errRequestInvalid
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, errRequestInvalid
			}
			if _, duplicate := object[key]; duplicate {
				return nil, errRequestInvalid
			}
			item, err := decodeStrictJSONValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			object[key] = item
		}
		end, err := decoder.Token()
		if err != nil || end != json.Delim('}') {
			return nil, errRequestInvalid
		}
		return object, nil
	case '[':
		items := make([]any, 0)
		for decoder.More() {
			item, err := decodeStrictJSONValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			items = append(items, item)
		}
		end, err := decoder.Token()
		if err != nil || end != json.Delim(']') {
			return nil, errRequestInvalid
		}
		return items, nil
	default:
		return nil, errRequestInvalid
	}
}

func validateRequestUnicode(raw []byte) error {
	if bytes.HasPrefix(raw, []byte{0xef, 0xbb, 0xbf}) || !utf8.Valid(raw) {
		return errRequestInvalid
	}
	inString := false
	for index := 0; index < len(raw); index++ {
		switch raw[index] {
		case '"':
			inString = !inString
		case '\\':
			if !inString || index+1 >= len(raw) {
				continue
			}
			index++
			if raw[index] != 'u' {
				continue
			}
			value, ok := fourHex(raw, index+1)
			if !ok {
				continue
			}
			index += 4
			if value >= 0xd800 && value <= 0xdbff {
				if index+6 >= len(raw) || raw[index+1] != '\\' || raw[index+2] != 'u' {
					return errRequestInvalid
				}
				low, ok := fourHex(raw, index+3)
				if !ok || low < 0xdc00 || low > 0xdfff {
					return errRequestInvalid
				}
				index += 6
			} else if value >= 0xdc00 && value <= 0xdfff {
				return errRequestInvalid
			}
		}
	}
	return nil
}

func fourHex(raw []byte, offset int) (uint16, bool) {
	if offset < 0 || offset+4 > len(raw) {
		return 0, false
	}
	var result uint16
	for _, value := range raw[offset : offset+4] {
		result <<= 4
		switch {
		case value >= '0' && value <= '9':
			result |= uint16(value - '0')
		case value >= 'a' && value <= 'f':
			result |= uint16(value-'a') + 10
		case value >= 'A' && value <= 'F':
			result |= uint16(value-'A') + 10
		default:
			return 0, false
		}
	}
	return result, true
}

func canonicalJSON(value any) ([]byte, error) {
	result, err := appendCanonicalJSON(nil, value)
	if err != nil {
		zero(result)
		return nil, err
	}
	return result, nil
}

func appendCanonicalJSON(destination []byte, value any) ([]byte, error) {
	switch typed := value.(type) {
	case nil:
		return append(destination, "null"...), nil
	case bool:
		if typed {
			return append(destination, "true"...), nil
		}
		return append(destination, "false"...), nil
	case string:
		return appendCanonicalJSONString(destination, typed), nil
	case json.Number:
		parsed, err := strconv.ParseInt(string(typed), 10, 64)
		if err != nil {
			return destination, errRequestInvalid
		}
		return strconv.AppendInt(destination, parsed, 10), nil
	case []any:
		destination = append(destination, '[')
		for index, item := range typed {
			if index > 0 {
				destination = append(destination, ',')
			}
			var err error
			destination, err = appendCanonicalJSON(destination, item)
			if err != nil {
				return destination, err
			}
		}
		return append(destination, ']'), nil
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		destination = append(destination, '{')
		for index, key := range keys {
			if index > 0 {
				destination = append(destination, ',')
			}
			destination = appendCanonicalJSONString(destination, key)
			destination = append(destination, ':')
			var err error
			destination, err = appendCanonicalJSON(destination, typed[key])
			if err != nil {
				return destination, err
			}
		}
		return append(destination, '}'), nil
	default:
		return destination, errRequestInvalid
	}
}

func appendCanonicalJSONString(destination []byte, value string) []byte {
	const hexadecimal = "0123456789abcdef"
	destination = append(destination, '"')
	for _, character := range value {
		switch character {
		case '"', '\\':
			destination = append(destination, '\\', byte(character))
		case '\b':
			destination = append(destination, '\\', 'b')
		case '\f':
			destination = append(destination, '\\', 'f')
		case '\n':
			destination = append(destination, '\\', 'n')
		case '\r':
			destination = append(destination, '\\', 'r')
		case '\t':
			destination = append(destination, '\\', 't')
		default:
			switch {
			case character >= 0x20 && character <= 0x7e:
				destination = append(destination, byte(character))
			case character <= 0xffff:
				destination = append(destination, '\\', 'u',
					hexadecimal[(character>>12)&0xf],
					hexadecimal[(character>>8)&0xf],
					hexadecimal[(character>>4)&0xf],
					hexadecimal[character&0xf],
				)
			default:
				adjusted := character - 0x10000
				high := rune(0xd800) + (adjusted >> 10)
				low := rune(0xdc00) + (adjusted & 0x3ff)
				for _, surrogate := range []rune{high, low} {
					destination = append(destination, '\\', 'u',
						hexadecimal[(surrogate>>12)&0xf],
						hexadecimal[(surrogate>>8)&0xf],
						hexadecimal[(surrogate>>4)&0xf],
						hexadecimal[surrogate&0xf],
					)
				}
			}
		}
	}
	return append(destination, '"')
}

func sha256Digest(value []byte) string {
	digest := sha256.Sum256(value)
	return "sha256:" + hex.EncodeToString(digest[:])
}
