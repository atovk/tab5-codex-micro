#pragma once

#include "FreeRTOS.h"

using TaskHandle_t=void*;
using TaskFunction_t=void (*)(void*);
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t function,const char* name,
                                uint32_t stack,void* argument,UBaseType_t priority,
                                TaskHandle_t* task,BaseType_t core);
BaseType_t xTaskNotifyGive(TaskHandle_t task);
uint32_t ulTaskNotifyTake(BaseType_t clear,TickType_t timeout);
