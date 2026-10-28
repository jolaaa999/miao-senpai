package config

import (
	"os"
	"path/filepath"
	"strconv"
)

type Config struct {
	Host         string
	Port         int
	ProjectRoot  string
	TokenSecret  string
	AdminUser    string
	AdminPass    string
	AdminDBPath  string
	MemoryDir    string
	CheckinDir   string
	AffectionDir string
	StickerDir   string
	DrawDir      string
	TrendsDir    string
	GroupFeaturesPath string
	// 新增页面用到的数据路径（全部只读）
	DiaryDir             string
	ShopDir              string
	CharacterCatalogPath string
	CharacterRefDir      string
	DrawTracePath        string
	// 人设卡目录：与 bot 侧 DL_SENPAI_PERSONA_CARD_DIR 指向同一处
	PersonaCardDir       string
}

func Load() Config {
	root := os.Getenv("QQBOT_ROOT")
	if root == "" {
		if wd, err := os.Getwd(); err == nil {
			root = findProjectRoot(wd)
		}
	}
	root, _ = filepath.Abs(root)

	port := 28473
	if v := os.Getenv("SENPAI_CONSOLE_PORT"); v != "" {
		if p, err := strconv.Atoi(v); err == nil {
			port = p
		}
	}

	host := os.Getenv("SENPAI_CONSOLE_HOST")
	if host == "" {
		host = "127.0.0.1"
	}

	secret := os.Getenv("SENPAI_CONSOLE_TOKEN_SECRET")
	if secret == "" {
		secret = "senpai-console-dev-secret-change-me"
	}

	user := os.Getenv("SENPAI_CONSOLE_ADMIN_USER")
	if user == "" {
		user = "admin"
	}

	pass := os.Getenv("SENPAI_CONSOLE_ADMIN_PASS")
	if pass == "" {
		pass = "senpai2026"
	}

	adminDB := filepath.Join(root, "data", "dl_senpai", "admin", "console.db")

	return Config{
		Host:        host,
		Port:        port,
		ProjectRoot: root,
		TokenSecret: secret,
		AdminUser:   user,
		AdminPass:   pass,
		AdminDBPath: adminDB,
		MemoryDir:   filepath.Join(root, "data", "dl_senpai", "memory"),
		CheckinDir:  filepath.Join(root, "data", "dl_senpai", "checkin"),
		AffectionDir: filepath.Join(root, "data", "dl_senpai", "affection"),
		StickerDir:  filepath.Join(root, "data", "dl_senpai", "stickers"),
		DrawDir:     filepath.Join(root, "data", "dl_senpai", "draw"),
		TrendsDir:   filepath.Join(root, "data", "dl_senpai", "trends"),
		GroupFeaturesPath: filepath.Join(root, "data", "dl_senpai", "admin", "group_features.json"),
		DiaryDir:             filepath.Join(root, "data", "dl_senpai", "diary"),
		ShopDir:              filepath.Join(root, "data", "dl_senpai", "shop"),
		CharacterCatalogPath: filepath.Join(root, "src", "plugins", "dl_senpai", "data", "characters.json"),
		CharacterRefDir:      filepath.Join(root, "data", "dl_senpai", "character_references"),
		DrawTracePath:        filepath.Join(root, "data", "dl_senpai", "draw_traces.jsonl"),
		PersonaCardDir:       filepath.Join(root, "data", "dl_senpai", "personas"),
	}
}

func findProjectRoot(start string) string {
	dir := start
	for i := 0; i < 6; i++ {
		if _, err := os.Stat(filepath.Join(dir, "bot.py")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return start
}

func (c Config) Addr() string {
	return c.Host + ":" + strconv.Itoa(c.Port)
}
