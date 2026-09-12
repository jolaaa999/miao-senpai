package main

import (
	"log"
	"os"
	"path/filepath"

	"github.com/gin-gonic/gin"
	"github.com/qqbot/dl-senpai-console/internal/config"
	"github.com/qqbot/dl-senpai-console/internal/handlers"
	"github.com/qqbot/dl-senpai-console/internal/models"
	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
)

func main() {
	cfg := config.Load()

	for _, dir := range []string{
		filepath.Dir(cfg.AdminDBPath),
		cfg.MemoryDir,
		cfg.CheckinDir,
		cfg.StickerDir,
		cfg.TrendsDir,
	} {
		if err := os.MkdirAll(dir, 0755); err != nil {
			log.Fatalf("mkdir %s: %v", dir, err)
		}
	}

	db, err := gorm.Open(sqlite.Open(cfg.AdminDBPath), &gorm.Config{})
	if err != nil {
		log.Fatalf("db open: %v", err)
	}
	if err := db.AutoMigrate(&models.AdminUser{}, &models.LoginAudit{}); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(corsMiddleware())

	h := handlers.New(cfg, db)
	h.RegisterRoutes(r)

	log.Printf("学姐控制台后端 → http://%s/x7k9-dl-senpai-console/v1", cfg.Addr())
	log.Printf("项目根: %s", cfg.ProjectRoot)
	if err := r.Run(cfg.Addr()); err != nil {
		log.Fatal(err)
	}
}

func corsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Header("Access-Control-Allow-Origin", "*")
		c.Header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
		c.Header("Access-Control-Allow-Headers", "Authorization, Content-Type")
		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(204)
			return
		}
		c.Next()
	}
}
