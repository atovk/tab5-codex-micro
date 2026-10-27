#pragma once

#include <stdint.h>

// Driver observations only. Successful DMA writes do not prove acoustic output.
namespace AudioProbe {
struct Snapshot {
  bool tracked=false, infoOk=false, enabled=false;
  int32_t infoResult=0, port=-1, direction=0, mode=0;
  uint32_t sourceHz=0, mclkHz=0, bclkHz=0, sampleRate=0;
  uint32_t enableCalls=0, enableErrors=0, writeCalls=0, writeErrors=0;
  uint32_t writeTimeouts=0, shortWrites=0, requestedBytes=0, writtenBytes=0;
  // M5Unified retries zero-timeout writes; these are not failed PCM output.
  uint32_t nonblockingRetries=0, blockingErrors=0, blockingShortWrites=0;
  uint32_t nonzeroSamples=0, peak=0;
  int32_t lastEnableResult=0, lastWriteResult=0;
  int32_t firstWriteError=0, lastWriteError=0;
  uint32_t otherEnableCalls=0, otherWriteCalls=0;
};
Snapshot snapshot();
}  // namespace AudioProbe
