package handlers

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/qqbot/dl-senpai-console/internal/services"
)

// EditFeatureSwitch 修改单个布尔开关，并写回 .env / .env.dev。
//
// 安全性：只允许受限的键名与 true/false/1/0 字面量（见 services.ValidateKey），
// 凭据类（*_API_KEY/_TOKEN/...）一律拒绝，避免把后台变成任意文件写入面。
//
// 落盘后由 bot 侧的 config_watcher 在数秒内自动热重载，无需重启。
func (h *Handler) EditFeatureSwitch(c *gin.Context) {
	key := c.Param("key")
	var body struct {
		Value string `json:"value"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	touched, err := h.configWriter.SetFlag(key, body.Value)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"key":        key,
		"value":      body.Value,
		"written_to": touched,
		// 提醒前端：写入后 bot 需数秒完成热重载
		"note": "已写入，bot 将在数秒内热重载生效",
	})
}

// EditableFeatureSwitches 列出可写开关，供前端渲染开关控件。
func (h *Handler) EditableFeatureSwitches(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"items": h.configWriter.ListEditable()})
}

// 保证 services 包被引用（ValidateKey/IsSensitiveKey 由 writer 内部使用）。
var _ = services.IsSensitiveKey
