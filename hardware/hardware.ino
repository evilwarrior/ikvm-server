/** IP-KVM Support:
 *  HID Keyboard Controlling
 *  HID Mouse Controlling
 *  ATX Power Supply Controlling
 */
#include <Keyboard.h>
#include <Mouse.h>
#include <string.h>

/* General settings */
#define BAUD 19200
#define MAX_KEY KEY_F24 // key with maximal value in Keyboard.h
#define WHEEL_DOWN_AMOUNT  1
#define WHEEL_UP_AMOUNT   -1

/* ATX settings */
#define PWR 6           // PWR button emulator pin
#define RST 9           // RST button emulator pin
#define ORI_PWR 2       // Original power button pin
#define ORI_RST 21      // Original reset button pin
#define PWR_DELAY 400   // Milliseconds, short press PWR button time
#define RST_DELAY 400   // Milliseconds, press RST button time
#define LPWR_DELAY 5000 // Milliseconds, long press PWR button time

/* Protocol settings */
#define MAGIC_HEAD  0x0f
#define MAGIC_TAIL  0xe0
#define CMD_RSV     0x00    // Reserved
#define CMD_KEY     0x10
#define CMD_TXT     0x11
#define CMD_KEY_CLR 0x12
#define CMD_CUR_CLK 0x20    // Mouse click
#define CMD_CUR_MV  0x21
#define CMD_CUR_SCR 0x22
#define CMD_CUR_CLR 0x23
#define CMD_PWR     0x31
#define CMD_RST     0x32
#define CMD_LPWR    0x33

#define KEY_RLS     0x00    // Key release command
#define KEY_PRS     0x01    // Key press command
#define CUR_RLS     0x00    // Mouse release command
#define CUR_PRS     0x01    // Mouse press command
#define WHEEL_DOWN  0x00
#define WHEEL_UP    0x01

#define MAGIC_AT    0
#define CMD_AT      2
#define SIZE_AT     3
#define PRESS_AT    4 // send key/mouse click command press flag position
#define KEY_AT      5 // send key command key position
#define CHAR_AT     4 // send text command position
#define BTN_AT      5 // mouse click command button position
#define X_AT        4 // mouse move command x-move position
#define Y_AT        5 // mouse move command y-move position
#define ORIENT_AT   4 // mouse scroll wheel command orientation position

#define HEAD_SIZE     4 // from magic to size bytes size
#define SND_KEY_SIZE  3 // send key message size
#define SND_CHAR_SIZE 2 // send character message size
#define CUR_CLK_SIZE  3 // send mouse click message size
#define CUR_MV_SIZE   3 // send mouse move message size
#define CUR_SCR_SIZE  2 // send mouse scroll wheel message size
#define MIN_SIZE      1

/* Receive buffer settings */
#define MSG_MAX_SIZE 8
unsigned char g_buf[MSG_MAX_SIZE];
int g_nBufCursor = 0;
bool g_bBufFull = false;

/* Asynchronous press PWR/RST buttons */
bool g_isSendPwr = false; // is PWR button pressed by emulation
bool g_isSendRst = false; // is RST button pressed by emulation
unsigned long g_pwrTimer = 0; // recording PWR button pressed time
unsigned long g_rstTimer = 0; // recording RST button pressed time
unsigned long g_pwrDelay = 0; // how long PWR Timer would delay (PWR_DELAY or LPWR_DELAY)

bool checksum(char msg[], int len)
{
    unsigned char b = 0;
    for (int i = 0; i < len; i++)
        b ^= msg[i];
    return b == 0;
}

void ReadByteToBuf()
{
    unsigned char ucByte = Serial1.read();

    if (g_bBufFull) // Full message received, skipped (should never encounter)
        return;

    if (g_nBufCursor > MSG_MAX_SIZE)
    { // reset buffer as overflow
        memset(g_buf, '\0', sizeof(g_buf));
        g_nBufCursor = 0;
        return;
    }
    if (g_nBufCursor == MAGIC_AT)
    {   // read magic first byte
        if (ucByte == MAGIC_HEAD)
        {
            g_buf[g_nBufCursor] = MAGIC_HEAD;
            g_nBufCursor++;
        }
        return;
    }

    if (g_nBufCursor == MAGIC_AT+1)
    {   // read magic second byte
        if (ucByte == MAGIC_TAIL)
        {
            g_buf[g_nBufCursor] = MAGIC_TAIL;
            g_nBufCursor++;
        }
        else
        {   // reset buffer as invalid magic
            memset(g_buf, '\0', CMD_AT);
            g_nBufCursor = 0;
        }
        return;
    }

    if (g_nBufCursor == CMD_AT)
    {   // read command byte
        switch (ucByte)
        {
            case CMD_KEY:
            case CMD_TXT:
            case CMD_KEY_CLR:
            case CMD_CUR_CLK:
            case CMD_CUR_MV:
            case CMD_CUR_SCR:
            case CMD_CUR_CLR:
            case CMD_PWR:
            case CMD_RST:
            case CMD_LPWR:
                g_buf[g_nBufCursor] = ucByte;
                g_nBufCursor++;
                break;
            default: // reset buffer as invalid command
                memset(g_buf, '\0', CMD_AT);
                g_nBufCursor = 0;
        }
        return;
    }

    if (g_nBufCursor == SIZE_AT)
    {   // read size byte
        if (ucByte < MIN_SIZE)
        { // reset buffer as invalid size
            memset(g_buf, '\0', SIZE_AT);
            g_nBufCursor = 0;
        }
        else
        {
            g_buf[g_nBufCursor] = ucByte;
            g_nBufCursor++;
        }
        return;
    }

    g_buf[g_nBufCursor] = ucByte;
    g_nBufCursor++;

    // now read size bytes
    if (g_nBufCursor >= g_buf[SIZE_AT]+HEAD_SIZE)
    {   // read to end, reset cursor and set full flag as true
        g_bBufFull = true;
    }
}

void SendKey()
{
    if (g_buf[SIZE_AT] < SND_KEY_SIZE || g_buf[KEY_AT] > MAX_KEY) // skip not enough size or invalid key value
        return;
    switch (g_buf[PRESS_AT])
    {
        case KEY_PRS: // emulate key press
            Keyboard.press(g_buf[KEY_AT]);
            break;
        case KEY_RLS: // emulate key release
            Keyboard.release(g_buf[KEY_AT]);
            break;
    }
}

void WriteChar()
{
    if (g_buf[SIZE_AT] < SND_CHAR_SIZE || g_buf[CHAR_AT] > MAX_KEY) // skip not enough size or invalid key value
        return;
    Keyboard.write(g_buf[CHAR_AT]); // only one character write, ignore others for preventing serial byte lost
}

void MouseClick()
{
    if (g_buf[SIZE_AT] < CUR_CLK_SIZE ||
      !(g_buf[BTN_AT] == MOUSE_LEFT ||
        g_buf[BTN_AT] == MOUSE_RIGHT ||
        g_buf[BTN_AT] == MOUSE_MIDDLE)
    ) // skip not enough size or invalid button value
        return;
    switch (g_buf[PRESS_AT])
    {
        case CUR_PRS: // emulate mouse button press
            Mouse.press(g_buf[BTN_AT]);
            break;
        case CUR_RLS: // emulate mouse button release
            Mouse.release(g_buf[BTN_AT]);
            break;
    }
}

void MoveMouse()
{
    if (g_buf[SIZE_AT] < CUR_MV_SIZE) // skip not enough size
        return;
    Mouse.move((char)g_buf[X_AT], (char)g_buf[Y_AT]);
}

void ScrollWheel()
{
    if (g_buf[SIZE_AT] < CUR_SCR_SIZE) // skip not enough size
        return;
    switch (g_buf[ORIENT_AT])
    {
        case WHEEL_DOWN:
            Mouse.move(0, 0, WHEEL_DOWN_AMOUNT);
            break;
        case WHEEL_UP:
            Mouse.move(0, 0, WHEEL_UP_AMOUNT);
            break;
    }
}

void PowerSignal(unsigned long ulDelay)
{
    if (!g_isSendPwr)
    {
        g_pwrTimer = millis();
        g_pwrDelay = ulDelay;
        g_isSendPwr = true;
        digitalWrite(PWR, HIGH);
    }
}

void ResetSignal()
{
    if (!g_isSendRst)
    {
        g_rstTimer = millis();
        g_isSendRst = true;
        digitalWrite(RST, HIGH);
    }
}

void AnalyzeByteFromBuf()
{
    if (!checksum(g_buf, g_nBufCursor)) // checksum failed
        g_buf[CMD_AT] = CMD_RSV; // skip switch below for setting g_bBufFull to false
    switch (g_buf[CMD_AT])
    {
        case CMD_KEY: // click a keyboard key
            SendKey();
            break;
        case CMD_TXT: // click a keyboard printable character
            WriteChar();
            break;
        case CMD_KEY_CLR: // release all pressed keyboard keys
            Keyboard.releaseAll();
            break;
        case CMD_CUR_CLK: // click a mouse button
            MouseClick();
            break;
        case CMD_CUR_MV: // move mouse
            MoveMouse();
            break;
        case CMD_CUR_SCR: // scroll the mouse wheel
            ScrollWheel();
            break;
        case CMD_CUR_CLR: // release all pressed mouse buttons
            Mouse.release(MOUSE_LEFT);
            Mouse.release(MOUSE_RIGHT);
            Mouse.release(MOUSE_MIDDLE);
            break;
        case CMD_PWR:   // emulate short press power button
            PowerSignal(PWR_DELAY);
            break;
        case CMD_RST:   // emulate press reset button
            ResetSignal();
            break;
        case CMD_LPWR:  // emulate long press power button
            PowerSignal(LPWR_DELAY);
            break;
    }
    // reset buffer, cursor and full flag
    memset(g_buf, '\0', sizeof(g_buf));
    g_nBufCursor = 0;
    g_bBufFull = false;
}

void setup()
{
    memset(g_buf, '\0', sizeof(g_buf)); // initialization buffer

    pinMode(LED_BUILTIN_RX, OUTPUT);
    // I/O Setup
    digitalWrite(PWR, LOW);
    digitalWrite(RST, LOW);
    pinMode(PWR, OUTPUT);
    pinMode(RST, OUTPUT);
    pinMode(ORI_PWR, INPUT);
    pinMode(ORI_RST, INPUT);
    digitalWrite(ORI_PWR, HIGH);
    digitalWrite(ORI_RST, HIGH);
    Serial1.begin(BAUD);  // hardware serial port
    Keyboard.begin();     // keyboard emulation
    Mouse.begin();        // mouse emulation
}

void loop()
{
    int nSt = digitalRead(PWR);
    unsigned long ulCurTime = millis();
    if (g_isSendPwr && ulCurTime-g_pwrTimer>g_pwrDelay)
    { // Check if board is pulled PWR button to HIGH
        digitalWrite(PWR, LOW);
        g_isSendPwr = false;
    }
    else if (!g_isSendPwr && digitalRead(ORI_PWR) == nSt)
    { // Transform original power button input into power button emulator output
        digitalWrite(PWR, nSt^0x01);
    }

    nSt = digitalRead(RST);
    if (g_isSendRst && ulCurTime-g_rstTimer>RST_DELAY)
    { // Check if board is pulled RST button to HIGH
        digitalWrite(RST, LOW);
        g_isSendRst = false;
    }
    else if (!g_isSendRst && digitalRead(ORI_RST) == nSt)
    { // Transform original reset button input into reset button emulator output
        digitalWrite(RST, nSt^0x01);
    }

    // Full message received, now analyze
    if (g_bBufFull)
        AnalyzeByteFromBuf();

    // Read from iKVM server
    if (Serial1.available() > 0)
    {
        digitalWrite(LED_BUILTIN_RX, LOW);  // LED on, blinkies
        ReadByteToBuf();
        digitalWrite(LED_BUILTIN_RX, HIGH); // LED off, blinkies
    }
}
