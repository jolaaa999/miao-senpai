package services

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// 角色特征的核验三态。含义见 data/dl_senpai/characters/README.md：
// 只有 human_verified 才能进提示词的硬约束段，其余只能是支撑性提示。
const (
	statusHumanVerified = "human_verified"
	statusReviewed      = "assistant_reviewed"
	statusUnverified    = "unverified"
)

// characterFieldLabels 把视觉特征的英文字段名翻成中文标签；
// 表里没有的新字段直接显示原名，避免新增字段在页面里看不见。
var characterFieldLabels = map[string]string{
	"race":        "族属/种族",
	"hair":        "头发",
	"eyes":        "眼睛",
	"horns":       "角",
	"ears":        "耳朵",
	"face":        "面部",
	"clothing":    "服装",
	"accessories": "配饰",
	"silhouette":  "体型",
	"palette":     "配色",
	"weapon":      "武器",
	"tail":        "尾巴",
	"wings":       "翅膀",
	"height":      "身高",
	"markings":    "特殊纹样",
}

// characterFieldOrder 决定页面里的展示顺序：先外观主干，再细节。
var characterFieldOrder = []string{
	"race", "hair", "eyes", "horns", "ears", "face",
	"clothing", "accessories", "silhouette", "palette",
	"weapon", "tail", "wings", "markings", "height",
}

type CharacterField struct {
	Key                string `json:"key"`
	Label              string `json:"label"`
	Value              string `json:"value"`
	VerificationStatus string `json:"verification_status"`
	VerificationMethod string `json:"verification_method"`
	VerifiedAt         string `json:"verified_at"`
	Note               string `json:"note"`
}

type CharacterProfile struct {
	Key                string           `json:"key"`
	CanonicalName      string           `json:"canonical_name"`
	LocalizedNames     []string         `json:"localized_names"`
	Aliases            []string         `json:"aliases"`
	Franchise          string           `json:"franchise"`
	FranchiseLocalized string           `json:"franchise_localized"`
	IdentityPriority   string           `json:"identity_priority"`
	Confidence         float64          `json:"confidence"`
	ProfileStatus      string           `json:"profile_status"`
	Fields             []CharacterField `json:"fields"`
	MustPreserve       []string         `json:"must_preserve"`
	CreativeFreedom    []string         `json:"creative_freedom"`
	NegativeNotes      []string         `json:"negative_notes"`
	HumanVerifiedCount int              `json:"human_verified_count"`
	ReviewedCount      int              `json:"reviewed_count"`
	UnverifiedCount    int              `json:"unverified_count"`
}

// CharacterReference：人工提供的身份参考图清单（manifest）里的一条。
type CharacterReference struct {
	Manifest           string `json:"manifest"`
	Character          string `json:"character"`
	LocalizedName      string `json:"localized_name"`
	File               string `json:"file"`
	Type               string `json:"type"`
	ReferenceRole      string `json:"reference_role"`
	VerificationStatus string `json:"verification_status"`
	Priority           int    `json:"priority"`
	SourceType         string `json:"source_type"`
	VerifiedAt         string `json:"verified_at"`
	Notes              string `json:"notes"`
}

// IdentityBenchmark：身份一致性的 A/B 实验产物（项目根 bench-*.json）。
// 每个文件是一套提示词策略跑出来的一批图，用来对比哪种策略更能保持角色长相。
type IdentityBenchmark struct {
	Strategy         string   `json:"strategy"`
	File             string   `json:"file"`
	ItemCount        int      `json:"item_count"`
	Models           []string `json:"models"`
	AvgPromptChars   int      `json:"avg_prompt_chars"`
	// HumanReferenceCount：这套策略里有几条提示词真正带了人工核验参考图。
	// 用条数而不是布尔值 —— 参考图缺失时 bot 会退回纯文本，4/20 这种部分覆盖必须能看见。
	HumanReferenceCount int    `json:"human_reference_count"`
	CreatedFrom      string   `json:"created_from"`
	CreatedTo        string   `json:"created_to"`
}

// ImageTrace：一次生图的溯源记录（data/dl_senpai/draw_traces.jsonl）。
type ImageTrace struct {
	TraceID          string `json:"trace_id"`
	Character        string `json:"character"`
	Franchise        string `json:"franchise"`
	Intent           string `json:"intent"`
	ReferenceMode    string `json:"reference_mode"`
	ReferenceCount   int    `json:"reference_count"`
	GenerationMode   string `json:"generation_mode"`
	Result           string `json:"result"`
	ElapsedMS        int    `json:"elapsed_ms"`
	FinalPromptChars int    `json:"final_prompt_chars"`
	OriginalPrompt   string `json:"original_prompt"`
	Ts               string `json:"ts"`
}

type CharacterSnapshot struct {
	Profiles   []CharacterProfile   `json:"profiles"`
	References []CharacterReference `json:"references"`
	Benchmarks []IdentityBenchmark  `json:"benchmarks"`
	Traces     []ImageTrace         `json:"traces"`
	Note       string               `json:"note"`
	Version    int                  `json:"version"`
	Catalog    string               `json:"catalog"`
}

type characterVisualField struct {
	Value              string `json:"value"`
	VerificationStatus string `json:"verification_status"`
	VerificationMethod string `json:"verification_method"`
	VerifiedAt         string `json:"verified_at"`
	Note               string `json:"note"`
	Verified           *bool  `json:"verified"`
}

type characterRaw struct {
	CanonicalName      string                          `json:"canonical_name"`
	LocalizedNames     []string                        `json:"localized_names"`
	Aliases            []string                        `json:"aliases"`
	Franchise          string                          `json:"franchise"`
	FranchiseLocalized string                          `json:"franchise_localized"`
	IdentityPriority   string                          `json:"identity_priority"`
	Confidence         float64                         `json:"confidence"`
	VisualIdentity     map[string]characterVisualField `json:"visual_identity"`
	MustPreserve       []string                        `json:"must_preserve"`
	CreativeFreedom    []string                        `json:"creative_freedom"`
	NegativeNotes      []string                        `json:"negative_notes"`
}

// characterFile：characters 在数据文件里是数组（按收录顺序排列），不是按名字索引的对象。
type characterFile struct {
	Version    int              `json:"version"`
	Note       string           `json:"note"`
	Characters []characterRaw   `json:"characters"`
}

type CharacterService struct {
	catalogPath   string
	referenceDir  string
	tracePath     string
	projectRoot   string
}

func NewCharacterService(catalogPath, referenceDir, tracePath, projectRoot string) *CharacterService {
	return &CharacterService{
		catalogPath:  catalogPath,
		referenceDir: referenceDir,
		tracePath:    tracePath,
		projectRoot:  projectRoot,
	}
}

// Get 汇总角色档案、参考图清单、A/B 实验与最近生图溯源。
func (s *CharacterService) Get(traceLimit int) (CharacterSnapshot, error) {
	snap := CharacterSnapshot{
		Profiles:   []CharacterProfile{},
		References: []CharacterReference{},
		Benchmarks: []IdentityBenchmark{},
		Traces:     []ImageTrace{},
		Catalog:    s.catalogPath,
	}
	if data, err := os.ReadFile(s.catalogPath); err == nil {
		var file characterFile
		if err := json.Unmarshal(data, &file); err != nil {
			return snap, err
		}
		snap.Note = file.Note
		snap.Version = file.Version
		for i, raw := range file.Characters {
			snap.Profiles = append(snap.Profiles, s.buildProfile(fmt.Sprintf("%d", i), raw))
		}
		// 用中文名排序，页面顺序稳定（map 遍历本身是乱序）
		sort.Slice(snap.Profiles, func(i, j int) bool {
			return s.profileLabel(snap.Profiles[i]) < s.profileLabel(snap.Profiles[j])
		})
	} else if !os.IsNotExist(err) {
		return snap, err
	}

	snap.References = s.loadReferences()
	snap.Benchmarks = s.loadBenchmarks()
	snap.Traces = s.loadTraces(traceLimit)
	return snap, nil
}

func (s *CharacterService) profileLabel(p CharacterProfile) string {
	if len(p.LocalizedNames) > 0 {
		return p.LocalizedNames[0]
	}
	return p.CanonicalName
}

func (s *CharacterService) buildProfile(key string, raw characterRaw) CharacterProfile {
	profile := CharacterProfile{
		Key:                key,
		CanonicalName:      raw.CanonicalName,
		LocalizedNames:     nonNil(raw.LocalizedNames),
		Aliases:            nonNil(raw.Aliases),
		Franchise:          raw.Franchise,
		FranchiseLocalized: raw.FranchiseLocalized,
		IdentityPriority:   raw.IdentityPriority,
		Confidence:         raw.Confidence,
		MustPreserve:       nonNil(raw.MustPreserve),
		CreativeFreedom:    nonNil(raw.CreativeFreedom),
		NegativeNotes:      nonNil(raw.NegativeNotes),
		Fields:             []CharacterField{},
	}
	for fieldKey, field := range raw.VisualIdentity {
		status := field.VerificationStatus
		if status == "" {
			// 老数据只有布尔 verified：true 视为人工核验过，false/缺省视为未核验
			if field.Verified != nil && *field.Verified {
				status = statusHumanVerified
			} else {
				status = statusUnverified
			}
		}
		label := characterFieldLabels[fieldKey]
		if label == "" {
			label = fieldKey
		}
		profile.Fields = append(profile.Fields, CharacterField{
			Key:                fieldKey,
			Label:              label,
			Value:              field.Value,
			VerificationStatus: status,
			VerificationMethod: field.VerificationMethod,
			VerifiedAt:         field.VerifiedAt,
			Note:               field.Note,
		})
		switch status {
		case statusHumanVerified:
			profile.HumanVerifiedCount++
		case statusReviewed:
			profile.ReviewedCount++
		default:
			profile.UnverifiedCount++
		}
	}
	sort.SliceStable(profile.Fields, func(i, j int) bool {
		return fieldRank(profile.Fields[i].Key) < fieldRank(profile.Fields[j].Key)
	})
	profile.ProfileStatus = profileStatusOf(profile)
	return profile
}

// fieldRank 给已知字段固定顺序，未知字段排到最后（同权重时按名字稳定排序）。
func fieldRank(key string) int {
	for i, k := range characterFieldOrder {
		if k == key {
			return i
		}
	}
	return len(characterFieldOrder)
}

// profileStatusOf 汇总整份档案的可信度：
// draft=全未核验，partial_verified=人工核验了一部分，verified=全部人工核验过。
func profileStatusOf(p CharacterProfile) string {
	human := p.HumanVerifiedCount
	if human == 0 {
		// 只有模型审阅、没人看过：不能算 draft（那是啥都没核），也不能算 verified
		if p.ReviewedCount > 0 {
			return "review_only"
		}
		if p.UnverifiedCount == 0 {
			return "empty"
		}
		return "draft"
	}
	if p.ReviewedCount == 0 && p.UnverifiedCount == 0 {
		return "verified"
	}
	return "partial_verified"
}

func nonNil(in []string) []string {
	if in == nil {
		return []string{}
	}
	return in
}

// loadReferences 读参考图清单。目录里只有 example 时不算配置过，返回空列表。
func (s *CharacterService) loadReferences() []CharacterReference {
	out := []CharacterReference{}
	entries, err := os.ReadDir(s.referenceDir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".json") || strings.Contains(name, "example") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(s.referenceDir, name))
		if err != nil {
			continue
		}
		var manifest struct {
			Character     string `json:"character"`
			LocalizedName string `json:"localized_name"`
			References    []struct {
				File               string `json:"file"`
				Type               string `json:"type"`
				ReferenceRole      string `json:"reference_role"`
				VerificationStatus string `json:"verification_status"`
				Priority           int    `json:"priority"`
				SourceType         string `json:"source_type"`
				VerifiedAt         string `json:"verified_at"`
				Notes              string `json:"notes"`
			} `json:"references"`
		}
		if err := json.Unmarshal(data, &manifest); err != nil {
			continue
		}
		for _, ref := range manifest.References {
			out = append(out, CharacterReference{
				Manifest:           name,
				Character:          manifest.Character,
				LocalizedName:      manifest.LocalizedName,
				File:               ref.File,
				Type:               ref.Type,
				ReferenceRole:      ref.ReferenceRole,
				VerificationStatus: ref.VerificationStatus,
				Priority:           ref.Priority,
				SourceType:         ref.SourceType,
				VerifiedAt:         ref.VerifiedAt,
				Notes:              ref.Notes,
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Priority > out[j].Priority })
	return out
}

// loadBenchmarks 扫项目根下的 bench-*.json，汇总每套策略的规模与是否带人工参考图。
func (s *CharacterService) loadBenchmarks() []IdentityBenchmark {
	out := []IdentityBenchmark{}
	paths, _ := filepath.Glob(filepath.Join(s.projectRoot, "bench-*.json"))
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var items []struct {
			Prompt    string `json:"prompt"`
			Model     string `json:"model"`
			CreatedAt string `json:"created_at"`
		}
		if err := json.Unmarshal(data, &items); err != nil {
			continue
		}
		file := filepath.Base(path)
		bench := IdentityBenchmark{
			Strategy: strings.TrimSuffix(strings.TrimPrefix(file, "bench-"), ".json"),
			File:     file,
			ItemCount: len(items),
			Models:   []string{},
		}
		modelSeen := map[string]struct{}{}
		totalChars := 0
		refHits := 0
		for _, it := range items {
			totalChars += len([]rune(it.Prompt))
			if strings.Contains(it.Prompt, "Human-verified reference image") {
				refHits++
			}
			if it.Model != "" {
				if _, ok := modelSeen[it.Model]; !ok {
					modelSeen[it.Model] = struct{}{}
					bench.Models = append(bench.Models, it.Model)
				}
			}
			if bench.CreatedFrom == "" || it.CreatedAt < bench.CreatedFrom {
				bench.CreatedFrom = it.CreatedAt
			}
			if it.CreatedAt > bench.CreatedTo {
				bench.CreatedTo = it.CreatedAt
			}
		}
		if len(items) > 0 {
			bench.AvgPromptChars = totalChars / len(items)
		}
		bench.HumanReferenceCount = refHits
		sort.Strings(bench.Models)
		out = append(out, bench)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Strategy < out[j].Strategy })
	return out
}

// loadTraces 取生图溯源日志的最后 limit 条（文件是追加写的，新的在末尾）。
func (s *CharacterService) loadTraces(limit int) []ImageTrace {
	out := []ImageTrace{}
	f, err := os.Open(s.tracePath)
	if err != nil {
		return out
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var raw struct {
			TraceID          string `json:"trace_id"`
			Character        string `json:"character"`
			Franchise        string `json:"franchise"`
			Intent           string `json:"intent"`
			ReferenceMode    string `json:"reference_mode"`
			ReferenceCount   int    `json:"reference_count"`
			GenerationMode   string `json:"generation_mode"`
			Result           string `json:"result"`
			ElapsedMS        int    `json:"elapsed_ms"`
			FinalPromptChars int    `json:"final_prompt_chars"`
			OriginalPrompt   string `json:"original_prompt"`
			Ts               string `json:"ts"`
		}
		if err := json.Unmarshal([]byte(line), &raw); err != nil {
			continue
		}
		out = append(out, ImageTrace{
			TraceID:          raw.TraceID,
			Character:        raw.Character,
			Franchise:        raw.Franchise,
			Intent:           raw.Intent,
			ReferenceMode:    raw.ReferenceMode,
			ReferenceCount:   raw.ReferenceCount,
			GenerationMode:   raw.GenerationMode,
			Result:           raw.Result,
			ElapsedMS:        raw.ElapsedMS,
			FinalPromptChars: raw.FinalPromptChars,
			OriginalPrompt:   raw.OriginalPrompt,
			Ts:               raw.Ts,
		})
	}
	// 倒序：页面看最新的在上
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out
}
