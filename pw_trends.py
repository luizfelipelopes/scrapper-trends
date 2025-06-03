import asyncio
import time
import requests
import re

from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        # Create a new page in the provided browser context
        page = await context.new_page()

        await page.goto('https://trends.google.com.br/trending?geo=BR&hl=pt-BR&category=4')
        await page.click('//*[@id="trend-table"]/div[1]/table/tbody[2]/tr[1]')
        href = await page.locator('//*[@id="yDmH0d"]/c-wiz/div/div[5]/div[1]/c-wiz/div/div[2]/div[2]/div[2]/div[4]/div/div[2]/div[1]/a').get_attribute('href')
        href2 = await page.locator('//*[@id="yDmH0d"]/c-wiz/div/div[5]/div[1]/c-wiz/div/div[2]/div[2]/div[2]/div[4]/div/div[2]/div[2]/a').get_attribute('href')
        href3 = await page.locator('//*[@id="yDmH0d"]/c-wiz/div/div[5]/div[1]/c-wiz/div/div[2]/div[2]/div[2]/div[4]/div/div[2]/div[3]/a').get_attribute('href')
        print(f'Link 1: {href}')
        print(f'Link 2: {href2}')
        print(f'Link 3: {href3}')

        await page.goto(href)

        title_locator = page.locator('h1').first
        title = await title_locator.text_content()

        safe_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', title)
        print(f'Título: {title} = {safe_title}')

        img_locator = page.locator('article img').first
        img_src = await img_locator.get_attribute('src')
        print(img_src)

        response = requests.get(img_src)

        if img_src:
            print(f'Imagem encontrada: {img_src}')
            
            # Faz o download da imagem
            response = requests.get(img_src)

            if response.status_code == 200:
                with open(f'covers/{safe_title}.jpg', 'wb') as f:
                    f.write(response.content)
                print('Imagem baixada com sucesso!')
            else:
                print(f'Falha ao baixar a imagem: Status {response.status_code}')
        else:
            print('Nenhuma imagem encontrada.')


        time.sleep(10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
