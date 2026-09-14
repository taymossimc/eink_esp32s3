/*****************************************************************************
* | File        :   EPD_12in48.c
* | Author      :   Waveshare team
* | Function    :   Electronic paper driver
* | Info     :
*----------------
* | This version:   V1.0
* | Date     :   2018-11-29
* | Info     :
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files(the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
******************************************************************************/
#include "EPD_13in3e.h"
#include "Debug.h"


// const UBYTE spiCsPin[2] = {
// 		SPI_CS0, SPI_CS1
// };
const UBYTE PSR_V[2] = {
	0xDF, 0x6B
};
const UBYTE PLL_V[1] = {
	0x08
};
const UBYTE PWR_V[6] = {
	0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38
};
const UBYTE POF_V[1] = {
	0x00
};
const UBYTE DRF_V[1] = {
	0x00
};
const UBYTE CDI_V[1] = {
	0x37
};
const UBYTE TCON_V[2] = {
	0x03, 0x03
};
const UBYTE TRES_V[4] = {
	0x04, 0xB0, 0x03, 0x20
};
const UBYTE CMD66_V[6] = {
	0x49, 0x55, 0x13, 0x5D, 0x05, 0x10
};
const UBYTE EN_BUF_V[1] = {
	0x07
};
const UBYTE CCSET_V[1] = {
	0x01
};
const UBYTE PWS_V[1] = {
	0x22
};
const UBYTE AN_TM_V[9] = {
	0x00, 0x0C, 0x0C, 0xD9, 0xDD, 0xDD, 0x15, 0x15, 0x55
};


const UBYTE AGID_V[1] = {
	0x10
};

const UBYTE BTST_P_V[2] = {
	0xE0, 0x20
};
const UBYTE BOOST_VDDP_EN_V[1] = {
	0x01
};
const UBYTE BTST_N_V[2] = {
	0xE0, 0x20
};
const UBYTE BUCK_BOOST_VDDN_V[1] = {
	0x01
};
const UBYTE TFT_VCOM_POWER_V[1] = {
	0x02
};
const UBYTE DCDC_V[3] = {
	0x44, 0x54, 0x00
};
const UBYTE POFS_M_V[4] = {
	0x00, 0xC0, 0x03, 0xA8
};
const UBYTE POFS_S_V[4] = {
	0x00, 0xC0, 0x03, 0x9A
};
const UBYTE CMDA4_V[9] = {
	0x03, 0x00, 0x01, 0x03, 0x00, 0x03, 0x00, 0x00, 0x00
};


void EPD_13IN3E_CS_ALL(UBYTE Value)
{
    DEV_Digital_Write(EPD_CS_M_PIN, Value);
    DEV_Digital_Write(EPD_CS_S_PIN, Value);
}


static void EPD_13IN3E_SPI_Sand(UBYTE Cmd, const UBYTE *buf, UDOUBLE Len)
{
    // Command: DC low
    DEV_Digital_Write(EPD_DC_PIN, 0);
    DEV_Delay_ms(300);
    DEV_SPI_WriteByte(Cmd);
    // Data: DC high
    if (buf != NULL && Len > 0) {
        DEV_Digital_Write(EPD_DC_PIN, 1);
        DEV_SPI_Write_nByte((UBYTE *)buf,Len);
    }
    // Reset DC low to match Python driver behaviour
    DEV_Digital_Write(EPD_DC_PIN, 0);
}


/******************************************************************************
function :	Software reset
parameter:
******************************************************************************/
static void EPD_13IN3E_Reset(void)
{
    // Generous timing (matches Pimoroni driver): needed to reliably wake the
    // panel from deep sleep, not just from a cold power-on.
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(50);
    DEV_Digital_Write(EPD_RST_PIN, 0);
    DEV_Delay_ms(100);
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(200);
}

/******************************************************************************
function :	send command
parameter:
     Reg : Command register
******************************************************************************/

void EPD_13IN3E_SendCommand(UBYTE Reg)
{
    // Ensure DC is low for command
    DEV_Digital_Write(EPD_DC_PIN, 0);
    DEV_SPI_WriteByte(Reg);
}

/******************************************************************************
function :	send data
parameter:
    Data : Write data
******************************************************************************/
void EPD_13IN3E_SendData(UBYTE Reg)
{
    // Ensure DC is high for data
    DEV_Digital_Write(EPD_DC_PIN, 1);
    DEV_SPI_WriteByte(Reg);
}
void EPD_13IN3E_SendData2(const UBYTE *buf, uint32_t Len)
{
    // Ensure DC is high for data
    DEV_Digital_Write(EPD_DC_PIN, 1);
    DEV_SPI_Write_nByte((UBYTE *)buf,Len);
}

/******************************************************************************
function :	Wait until the busy_pin goes LOW
parameter:
******************************************************************************/
static bool EPD_13IN3E_ReadBusyH(void)
{
    const uint32_t start = millis();
    Serial.printf("BUSY wait: initial=%s\n",
                  DEV_Digital_Read(EPD_BUSY_PIN) ? "HIGH (idle)" : "LOW (busy)");
	while(!DEV_Digital_Read(EPD_BUSY_PIN)) {      //LOW: busy, HIGH: idle
        DEV_Delay_ms(10);
        if (millis() - start > 120000) {
            Serial.println("ERROR: BUSY remained LOW for 120 seconds");
            return false;
        }
    }
	DEV_Delay_ms(20);
    Serial.printf("BUSY idle after %lu ms\n",
                  static_cast<unsigned long>(millis() - start));
    return true;
}

static bool EPD_13IN3E_RefreshStarted(uint32_t window_ms)
{
    const uint32_t start = millis();
    while (millis() - start < window_ms) {
        if (!DEV_Digital_Read(EPD_BUSY_PIN)) {
            Serial.println("BUSY asserted LOW; panel is refreshing");
            return true;
        }
        DEV_Delay_ms(10);
    }
    return false;
}

static bool EPD_13IN3E_WaitRefreshComplete(void)
{
    const uint32_t start = millis();
    while (!DEV_Digital_Read(EPD_BUSY_PIN)) {
        if (millis() - start > 65000) {
            Serial.println("ERROR: display refresh timed out after 65 seconds");
            return false;
        }
        DEV_Delay_ms(100);
    }
    Serial.printf("Refresh completed in %lu ms\n",
                  static_cast<unsigned long>(millis() - start));
    return true;
}


/******************************************************************************
function :  Turn On Display
parameter:
******************************************************************************/
bool EPD_13IN3E_TurnOnDisplay(void)
{
    printf("Write PON \r\n");
    EPD_13IN3E_CS_ALL(0);
    EPD_13IN3E_SPI_Sand(PON, NULL, 0); // POWER_ON with 300ms DC setup delay
    EPD_13IN3E_CS_ALL(1);
    if (!EPD_13IN3E_ReadBusyH()) {
        return false;
    }

    DEV_Delay_ms(200);
    printf("Write DRF \r\n");
    EPD_13IN3E_CS_ALL(0);
    EPD_13IN3E_SPI_Sand(DRF, DRF_V, sizeof(DRF_V));
    EPD_13IN3E_CS_ALL(1);

    // Single DRF, generous window (Pimoroni style): the panel can take a
    // while to assert BUSY, and re-sending DRF mid-preparation can disturb it.
    const bool started = EPD_13IN3E_RefreshStarted(65000);
    if (started) {
        if (!EPD_13IN3E_WaitRefreshComplete()) {
            return false;
        }
    } else {
        Serial.println("ERROR: panel did not assert BUSY after refresh command");
        // Do NOT send POF here: powering off mid-refresh can latch the panel
        // into a fault state that only full power removal recovers.
        return false;
    }

    DEV_Delay_ms(200);
    printf("Write POF \r\n");
    EPD_13IN3E_CS_ALL(0);
    EPD_13IN3E_SPI_Sand(POF, POF_V, sizeof(POF_V));
    EPD_13IN3E_CS_ALL(1);

    if (!EPD_13IN3E_ReadBusyH()) {
        return false;
    }
    DEV_Delay_ms(200);
    printf("Display Done!! \r\n");
    return true;
}

/******************************************************************************
function :	Initialize the e-Paper register
parameter:
******************************************************************************/
void EPD_13IN3E_Init(void)
{
	EPD_13IN3E_Reset();
    EPD_13IN3E_ReadBusyH();

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(AN_TM, AN_TM_V, sizeof(AN_TM_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(CMD66, CMD66_V, sizeof(CMD66_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(PSR, PSR_V, sizeof(PSR_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(0xA5, DCDC_V, sizeof(DCDC_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(PLL, PLL_V, sizeof(PLL_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(CDI, CDI_V, sizeof(CDI_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(TCON, TCON_V, sizeof(TCON_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(0x03, POFS_M_V, sizeof(POFS_M_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_S_PIN, 0);
	EPD_13IN3E_SPI_Sand(0x03, POFS_S_V, sizeof(POFS_S_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(AGID, AGID_V, sizeof(AGID_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(PWS, PWS_V, sizeof(PWS_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(CCSET, CCSET_V, sizeof(CCSET_V));
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_CS_ALL(0);
	EPD_13IN3E_SPI_Sand(TRES, TRES_V, sizeof(TRES_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(0xA4, CMDA4_V, sizeof(CMDA4_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(PWR_epd, PWR_V, sizeof(PWR_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(EN_BUF, EN_BUF_V, sizeof(EN_BUF_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(BTST_P, BTST_P_V, sizeof(BTST_P_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(BOOST_VDDP_EN, BOOST_VDDP_EN_V, sizeof(BOOST_VDDP_EN_V));
    EPD_13IN3E_CS_ALL(1);
	
    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(BTST_N, BTST_N_V, sizeof(BTST_N_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(BUCK_BOOST_VDDN, BUCK_BOOST_VDDN_V, sizeof(BUCK_BOOST_VDDN_V));
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
	EPD_13IN3E_SPI_Sand(TFT_VCOM_POWER, TFT_VCOM_POWER_V, sizeof(TFT_VCOM_POWER_V));
    EPD_13IN3E_CS_ALL(1);
    
}

/******************************************************************************
function :  Clear screen
parameter:
******************************************************************************/
void EPD_13IN3E_Clear(UBYTE color)
{
    UDOUBLE Width, Height;
    UBYTE Color;
    Width = (EPD_13IN3E_WIDTH % 2 == 0)? (EPD_13IN3E_WIDTH / 2 ): (EPD_13IN3E_WIDTH / 2 + 1);
    Height = EPD_13IN3E_HEIGHT;
    Color = (color<<4)|color;

    UBYTE buf[Width/2];

    for (UDOUBLE j = 0; j < Width/2; j++) {
        buf[j] = Color;
    }

    DEV_Digital_Write(EPD_CS_M_PIN, 0);
    EPD_13IN3E_SPI_Sand(DTM, NULL, 0); // DTM with 300ms DC setup delay
    for (UDOUBLE j = 0; j < EPD_13IN3E_HEIGHT; j++) {
        EPD_13IN3E_SendData2(buf, Width/2);
        DEV_Delay_ms(1);
    }
    EPD_13IN3E_CS_ALL(1);

    DEV_Digital_Write(EPD_CS_S_PIN, 0);
    EPD_13IN3E_SPI_Sand(DTM, NULL, 0); // DTM with 300ms DC setup delay
    for (UDOUBLE j = 0; j < EPD_13IN3E_HEIGHT; j++) {
        EPD_13IN3E_SendData2(buf, Width/2);
        DEV_Delay_ms(1);
    }
    EPD_13IN3E_CS_ALL(1);

    EPD_13IN3E_TurnOnDisplay();
}


void EPD_13IN3E_Display(const UBYTE *Image)
{
    EPD_13IN3E_TurnOnDisplay();
}


void EPD_13IN3E_DisplayPart(const UBYTE *Image, UWORD xstart, UWORD ystart, UWORD image_width, UWORD image_heigh)
{
    EPD_13IN3E_TurnOnDisplay();
}



void EPD_13IN3E_Show6Block(void)
{
    EPD_13IN3E_TurnOnDisplay();
}


/******************************************************************************
function :  Enter sleep mode
parameter:
******************************************************************************/
void EPD_13IN3E_Sleep(void)
{
    static const UBYTE DSLP_V[1] = {0xA5};
    EPD_13IN3E_CS_ALL(0);
    EPD_13IN3E_SPI_Sand(0x07, DSLP_V, sizeof(DSLP_V)); // DEEP_SLEEP
    EPD_13IN3E_CS_ALL(1);
}





