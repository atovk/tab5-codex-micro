#pragma once

#include <cstddef>
#include <cstdint>

using esp_err_t=int32_t;
using i2s_chan_handle_t=void*;
constexpr esp_err_t ESP_OK=0,ESP_FAIL=-1,ESP_ERR_INVALID_SIZE=0x104,
                    ESP_ERR_INVALID_STATE=0x103,ESP_ERR_NO_MEM=0x101;
constexpr int I2S_NUM_1=1,I2S_ROLE_SLAVE=1,I2S_DATA_BIT_WIDTH_16BIT=16,
              I2S_SLOT_MODE_STEREO=2;
constexpr int GPIO_NUM_NC=-1,GPIO_NUM_27=27,GPIO_NUM_28=28,GPIO_NUM_29=29;
struct i2s_chan_config_t {
  int id,role;
  uint32_t dma_desc_num=0,dma_frame_num=0;
};
struct i2s_std_clk_config_t { uint32_t sample_rate_hz=0; };
struct i2s_std_slot_config_t { int data_bit_width=0,slot_mode=0; };
struct i2s_std_gpio_config_t { int mclk=0,bclk=0,ws=0,dout=0,din=0; };
struct i2s_std_config_t {
  i2s_std_clk_config_t clk_cfg;
  i2s_std_slot_config_t slot_cfg;
  i2s_std_gpio_config_t gpio_cfg;
};
inline i2s_chan_config_t I2S_CHANNEL_DEFAULT_CONFIG(int id,int role) { return {id,role,0,0}; }
inline i2s_std_clk_config_t I2S_STD_CLK_DEFAULT_CONFIG(uint32_t rate) { return {rate}; }
inline i2s_std_slot_config_t I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(int width,int slots) {
  return {width,slots};
}
esp_err_t i2s_new_channel(const i2s_chan_config_t* config,i2s_chan_handle_t* tx,
                          i2s_chan_handle_t* rx);
esp_err_t i2s_del_channel(i2s_chan_handle_t channel);
esp_err_t i2s_channel_init_std_mode(i2s_chan_handle_t channel,const i2s_std_config_t* config);
esp_err_t i2s_channel_enable(i2s_chan_handle_t channel);
esp_err_t i2s_channel_disable(i2s_chan_handle_t channel);
esp_err_t i2s_channel_read(i2s_chan_handle_t channel,void* destination,size_t capacity,
                         size_t* received,uint32_t timeout);
